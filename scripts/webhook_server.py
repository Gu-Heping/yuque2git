#!/usr/bin/env python3
"""
yuque2git Webhook 服务：接收语雀 Webhook，写本地 Markdown + Git commit，支持智能推送（LLM / OpenClaw）。
"""
import argparse
import asyncio
import difflib
import json
import logging
import os
import smtplib
import subprocess
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 配置 ---
YUQUE_TOKEN = os.environ.get("YUQUE_TOKEN", "")
YUQUE_BASE_URL = os.environ.get("YUQUE_BASE_URL", "https://nova.yuque.com/api/v2").rstrip("/")
OUTPUT_DIR: Optional[Path] = None
PUSH_DECISION_MODE = os.environ.get("PUSH_DECISION_MODE", "llm")  # llm | openclaw
OPENCLAW_CALLBACK_URL = os.environ.get("OPENCLAW_CALLBACK_URL", "").strip() or os.environ.get("PUSH_CALLBACK_URL", "").strip()
OPENCLAW_HOOKS_TOKEN = os.environ.get("OPENCLAW_HOOKS_TOKEN", "").strip()
YUQUE2GIT_PUBLIC_URL = os.environ.get("YUQUE2GIT_PUBLIC_URL", "").strip()
NOTIFY_URL = os.environ.get("NOTIFY_URL", "").strip()
LAST_PUSH_FILE = ".yuque-last-push.json"  # key: yuque_id (str), value: commit hash
IDX_FILE = ".yuque-id-to-path.json"  # key: yuque_id (str), value: rel_path，用于文档移动时 diff 查旧路径
# 邮件推送（可选）：SMTP 配置，全部设置后则在判定推送时发邮件
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip() or os.environ.get("SMTP_PASS", "").strip()
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "").strip()
EMAIL_TO = [s.strip() for s in os.environ.get("EMAIL_TO", "").split(",") if s.strip()]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_CHAT_ENDPOINT = os.environ.get("OPENAI_CHAT_ENDPOINT", "chat/completions").strip().lstrip("/")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
# 自定义 API 认证：OPENAI_AUTH_HEADER_NAME 与 OPENAI_AUTH_HEADER_VALUE 同时设置时优先使用；否则用 Authorization: Bearer OPENAI_API_KEY
OPENAI_AUTH_HEADER_NAME = os.environ.get("OPENAI_AUTH_HEADER_NAME", "").strip()
OPENAI_AUTH_HEADER_VALUE = os.environ.get("OPENAI_AUTH_HEADER_VALUE", "").strip()
ENABLE_UPDATE_SUMMARY = os.environ.get("ENABLE_UPDATE_SUMMARY", "true").lower() in ("1", "true", "yes")
GIT_PUSH_ON_PUSH = os.environ.get("GIT_PUSH_ON_PUSH", "false").lower() in ("1", "true", "yes")
DIFF_MAX_CHARS = int(os.environ.get("DIFF_MAX_CHARS", "6000"))  # 截断 diff 给 LLM 的长度
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "25.0"))
ENABLE_BODY_ONLY_DIFF = os.environ.get("ENABLE_BODY_ONLY_DIFF", "true").lower() in ("1", "true", "yes")  # 只对正文做 diff，省 token


# --- Pydantic 模型（与语雀 Webhook 兼容）---
class WebhookBook(BaseModel):
    id: int
    slug: str
    name: str
    description: Optional[str] = None


class WebhookUser(BaseModel):
    id: int
    login: str
    name: str
    avatar_url: Optional[str] = None


class WebhookData(BaseModel):
    action_type: str
    id: int
    user_id: Optional[int] = None
    actor_id: Optional[int] = None
    slug: Optional[str] = None
    title: Optional[str] = None
    book: Optional[WebhookBook] = None
    actor: Optional[WebhookUser] = None


class WebhookPayload(BaseModel):
    data: WebhookData


class MarkPushedBody(BaseModel):
    yuque_id: int
    commit: str
    repo_slug: Optional[str] = None
    doc_slug: Optional[str] = None


# --- 语雀 API 客户端 ---
class YuqueClient:
    def __init__(self):
        self.base_url = YUQUE_BASE_URL
        self.headers = {
            "X-Auth-Token": YUQUE_TOKEN,
            "User-Agent": "yuque2git/1.0",
            "Content-Type": "application/json",
        }

    async def get_repo_toc(self, repo_id: int) -> List[Dict[str, Any]]:
        async with httpx.AsyncClient(headers=self.headers, timeout=30.0) as client:
            r = await client.get(f"{self.base_url}/repos/{repo_id}/toc")
            r.raise_for_status()
            return r.json().get("data", [])

    async def get_doc_detail(self, repo_id: int, slug: str) -> Optional[Dict[str, Any]]:
        async with httpx.AsyncClient(headers=self.headers, timeout=30.0) as client:
            r = await client.get(f"{self.base_url}/repos/{repo_id}/docs/{slug}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json().get("data", {})


def _slug_safe(s: str) -> str:
    for c in r'/\:*?"<>|':
        s = s.replace(c, "_")
    return s.strip() or "untitled"


def _parent_path_from_toc(toc_list: List[Dict], doc_id: int, doc_slug: Optional[str]) -> str:
    """根据 TOC 和文档 id/slug 得到父路径（用于目录层级），根层返回 ''。"""
    by_uuid: Dict[str, Dict] = {n["uuid"]: n for n in toc_list if n.get("uuid")}
    by_id: Dict[int, Dict] = {}
    for n in toc_list:
        i = n.get("id")
        if i is not None and (isinstance(i, int) or (isinstance(i, str) and i.isdigit())):
            by_id[int(i)] = n

    def slug_or_title(node: Dict) -> str:
        u = node.get("url") or node.get("slug")
        if u:
            return _slug_safe(str(u))
        return _slug_safe(node.get("title", "") or node.get("uuid", ""))

    def ancestors(node: Dict) -> List[Dict]:
        out: List[Dict] = []
        pu = node.get("parent_uuid")
        while pu:
            p = by_uuid.get(pu)
            if not p:
                break
            out.append(p)
            pu = p.get("parent_uuid")
        out.reverse()
        return out

    target = by_id.get(doc_id)
    if not target and doc_slug:
        for n in toc_list:
            if n.get("url") == doc_slug or n.get("slug") == doc_slug:
                target = n
                break
    if not target:
        return ""

    parts = [slug_or_title(p) for p in ancestors(target)]
    return "/".join(parts) if parts else ""


def _build_md(detail: Dict[str, Any], author_name: str = "") -> str:
    """frontmatter + 元数据表格 + 正文。"""
    fm = {k: v for k, v in detail.items() if k not in ("body", "body_html") and v is not None}
    created = detail.get("created_at") or ""
    updated = detail.get("updated_at") or detail.get("content_updated_at") or ""
    if isinstance(created, str) and "T" in created:
        created = created.replace("Z", "+00:00")[:19].replace("T", " ")
    if isinstance(updated, str) and "T" in updated:
        updated = updated.replace("Z", "+00:00")[:19].replace("T", " ")

    yaml_block = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
    table_cell = (lambda x: x.replace("|", "\\|").replace("\n", " ").strip() if x else "")
    author_display = table_cell(author_name or str(detail.get("user_id") or ""))

    md = "---\n" + yaml_block + "\n---\n\n"
    md += "| 作者 | 创建时间 | 更新时间 |\n|------|----------|----------|\n"
    md += f"| {author_display} | {table_cell(str(created))} | {table_cell(str(updated))} |\n\n"
    md += (detail.get("body") or "").strip()
    if not md.endswith("\n"):
        md += "\n"
    return md


def _ensure_git(output_dir: Path) -> None:
    if not (output_dir / ".git").exists():
        subprocess.run(["git", "init"], cwd=output_dir, check=True, capture_output=True)
        logger.info("git init in %s", output_dir)


def _git_add_commit(output_dir: Path, paths: List[str], message: str) -> Optional[str]:
    if not paths:
        return None
    subprocess.run(["git", "add", "--"] + paths, cwd=output_dir, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=output_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "nothing to commit" not in (result.stderr or "").lower():
        logger.warning("git commit: %s", result.stderr)
        return None
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=output_dir, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _read_last_push(output_dir: Path) -> Dict[str, str]:
    p = output_dir / LAST_PUSH_FILE
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_last_push(output_dir: Path, data: Dict[str, str]) -> None:
    p = output_dir / LAST_PUSH_FILE
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_index(output_dir: Path) -> Dict[str, str]:
    """yuque_id -> rel_path"""
    p = output_dir / IDX_FILE
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_index(output_dir: Path, data: Dict[str, str]) -> None:
    p = output_dir / IDX_FILE
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _find_doc_path_by_yuque_id(output_dir: Path, repo_slug: str, yuque_id: int) -> Optional[Path]:
    """在 repo_slug 下按 frontmatter 的 yuque_id 查找 .md 文件。"""
    repo_dir = output_dir / _slug_safe(repo_slug)
    if not repo_dir.is_dir():
        return None
    for md in repo_dir.rglob("*.md"):
        if md.name == ".md":
            continue
        try:
            raw = md.read_text(encoding="utf-8")
            if raw.startswith("---"):
                end = raw.index("---", 3) if "---" in raw[3:] else -1
                if end > 0:
                    fm = yaml.safe_load(raw[3:end])
                    if fm and fm.get("yuque_id") == yuque_id:
                        return md
        except Exception:
            continue
    return None


def _extract_body(md_text: str) -> str:
    """从 Markdown 中取出正文：去掉 YAML frontmatter 与元数据表格。"""
    if not md_text or "---" not in md_text:
        return md_text
    parts = md_text.split("---", 2)
    if len(parts) < 3:
        return md_text
    after = parts[2].lstrip("\n")
    lines = after.split("\n")
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith("|")):
        i += 1
    return "\n".join(lines[i:]).strip()


def _get_index_at_commit(output_dir: Path, commit: str) -> Dict[str, str]:
    """从指定 commit 读取 .yuque-id-to-path.json。"""
    r = subprocess.run(
        ["git", "show", f"{commit}:{IDX_FILE}"],
        cwd=output_dir,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or not r.stdout:
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def _get_diff(
    output_dir: Path,
    base_commit: Optional[str],
    rel_path: str,
    yuque_id: Optional[int] = None,
    body_only: bool = True,
) -> str:
    """生成「上次推送 → 当前」的 diff；body_only 时只对正文做 diff 以省 token。"""
    def _two_file_diff(old_c: str, new_c: str, from_f: str, to_f: str) -> str:
        if body_only:
            old_c = _extract_body(old_c)
            new_c = _extract_body(new_c)
        u = difflib.unified_diff(
            old_c.splitlines(keepends=True),
            new_c.splitlines(keepends=True),
            fromfile=from_f,
            tofile=to_f,
            lineterm="",
        )
        d = "".join(u)
        if len(d) > DIFF_MAX_CHARS:
            d = d[:DIFF_MAX_CHARS] + "\n... (已截断)"
        return d

    if not base_commit:
        full_path = output_dir / rel_path
        if full_path.exists():
            text = full_path.read_text(encoding="utf-8")
            if body_only:
                text = _extract_body(text)
            return f"[首次同步或从未推送]\n\n{text[:4000]}" + ("..." if len(text) > 4000 else "")
        return "[无基准，当前文件不存在]"

    old_path: Optional[str] = None
    if yuque_id is not None:
        idx = _get_index_at_commit(output_dir, base_commit)
        old_path = idx.get(str(yuque_id))
    if old_path and old_path != rel_path:
        r_old = subprocess.run(
            ["git", "show", f"{base_commit}:{old_path}"],
            cwd=output_dir,
            capture_output=True,
            text=True,
        )
        new_path = output_dir / rel_path
        new_content = new_path.read_text(encoding="utf-8") if new_path.exists() else ""
        old_content = (r_old.stdout or "") if r_old.returncode == 0 else ""
        diff = _two_file_diff(old_content, new_content, old_path, rel_path)
        if not diff.strip():
            return "[文档移动，内容无变更]"
        return diff

    if body_only:
        r_old = subprocess.run(
            ["git", "show", f"{base_commit}:{rel_path}"],
            cwd=output_dir,
            capture_output=True,
            text=True,
        )
        old_content = (r_old.stdout or "") if r_old.returncode == 0 else ""
        new_path = output_dir / rel_path
        new_content = new_path.read_text(encoding="utf-8") if new_path.exists() else ""
        diff = _two_file_diff(old_content, new_content, rel_path, rel_path)
        return diff or "[无文本变更]"

    r = subprocess.run(
        ["git", "diff", base_commit, "HEAD", "--", rel_path],
        cwd=output_dir,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        full_path = output_dir / rel_path
        if full_path.exists():
            text = full_path.read_text(encoding="utf-8")
            return f"[与基准 commit 路径可能不同]\n\n{text[:4000]}" + ("..." if len(text) > 4000 else "")
        return "[无法生成 diff]"
    diff = (r.stdout or "").strip()
    if len(diff) > DIFF_MAX_CHARS:
        diff = diff[:DIFF_MAX_CHARS] + "\n... (已截断)"
    return diff or "[无文本变更]"


async def _llm_should_push(diff: str, title: str, repo_name: str) -> tuple[bool, Optional[str]]:
    """调用 LLM 判断是否推送，返回 (should_push, update_summary)。失败时保守返回 (False, None)。"""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set, skip push decision")
        return False, None
    summary_instruction = ""
    if ENABLE_UPDATE_SUMMARY:
        summary_instruction = ' 同时用 1～3 句话生成 "update_summary" 字段，供订阅者快速了解本次变更（若无实质内容可留空）。'
    prompt = f"""你是一个文档推送决策助手。给定「语雀文档自上次推送以来的 diff」和文档标题、知识库名，判断本次变更是否值得向订阅者推送通知。

规则：仅当变更对读者有实质价值（如新增重要内容、修正错误、结构调整）时才返回 should_push=true；仅格式/标点/时间戳等小改动可返回 false。

知识库：{repo_name}
文档标题：{title}

Diff（自上次推送以来的变更）：
---
{diff}
---

请严格返回一个 JSON 对象，且仅此对象，不要其他文字。字段：
- should_push: boolean
- reason: string（简短理由）
{'- update_summary: string（1～3 句变更摘要，可选）' if ENABLE_UPDATE_SUMMARY else ''}
"""
    messages = [{"role": "user", "content": prompt}]
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 500,
    }
    url = f"{OPENAI_BASE_URL}/{OPENAI_CHAT_ENDPOINT}"
    if OPENAI_AUTH_HEADER_NAME and OPENAI_AUTH_HEADER_VALUE:
        headers = {OPENAI_AUTH_HEADER_NAME: OPENAI_AUTH_HEADER_VALUE, "Content-Type": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.warning("LLM request failed: %s", e)
        return False, None
    content = content.strip()
    if content.startswith("```"):
        for sep in ("```json", "```"):
            if sep in content:
                content = content.split(sep, 1)[-1].rsplit("```", 1)[0].strip()
                break
    try:
        obj = json.loads(content)
        should = bool(obj.get("should_push", False))
        summary = (obj.get("update_summary") or "").strip() if ENABLE_UPDATE_SUMMARY else None
        return should, summary
    except json.JSONDecodeError as e:
        logger.warning("LLM response not valid JSON: %s", e)
        return False, None


def _update_last_push_for(output_dir: Path, yuque_id: int, commit: str) -> None:
    data = _read_last_push(output_dir)
    data[str(yuque_id)] = commit
    _write_last_push(output_dir, data)


async def _notify_push(
    yuque_id: int,
    repo_slug: str,
    doc_slug: str,
    title: str,
    repo_name: str,
    commit: str,
    update_summary: Optional[str],
) -> None:
    """向 NOTIFY_URL 发送 POST，payload 含推送信息。"""
    if not NOTIFY_URL:
        return
    body = {
        "yuque_id": yuque_id,
        "repo_slug": repo_slug,
        "doc_slug": doc_slug,
        "title": title,
        "repo_name": repo_name,
        "commit": commit,
        "update_summary": update_summary or "",
        "action": "pushed",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(NOTIFY_URL, json=body)
    except Exception as e:
        logger.warning("NOTIFY_URL POST failed: %s", e)


def _send_email_push_sync(
    title: str,
    repo_name: str,
    repo_slug: str,
    doc_slug: str,
    update_summary: Optional[str],
    commit: str,
) -> None:
    """同步发送邮件（在判定推送后），使用 SMTP。"""
    if not SMTP_HOST or not EMAIL_FROM or not EMAIL_TO:
        return
    subject = f"[yuque2git] 文档更新: {title}"
    body_plain = f"""知识库：{repo_name}
文档标题：{title}
仓库/文档：{repo_slug} / {doc_slug}
Commit：{commit}

"""
    if update_summary:
        body_plain += f"变更摘要：\n{update_summary}\n"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(body_plain, "plain", "utf-8"))
    try:
        if SMTP_USE_TLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                if SMTP_USER and SMTP_PASSWORD:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                if SMTP_USER and SMTP_PASSWORD:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    except Exception as e:
        logger.warning("Email send failed: %s", e)


async def _send_email_push(
    title: str,
    repo_name: str,
    repo_slug: str,
    doc_slug: str,
    update_summary: Optional[str],
    commit: str,
) -> None:
    """异步封装：在线程中执行发邮件，不阻塞事件循环。"""
    if not SMTP_HOST or not EMAIL_FROM or not EMAIL_TO:
        return
    await asyncio.to_thread(
        _send_email_push_sync,
        title,
        repo_name,
        repo_slug,
        doc_slug,
        update_summary,
        commit,
    )


def _is_openclaw_hooks_agent_url(url: str) -> bool:
    """是否指向 OpenClaw Gateway 的 /hooks/agent 入口。"""
    u = (url or "").rstrip("/")
    return u.endswith("/hooks/agent")


async def _openclaw_callback(
    yuque_id: int,
    repo_slug: str,
    doc_slug: str,
    title: str,
    repo_name: str,
    diff: str,
    commit: str,
) -> None:
    """将「待判定」事件 POST 到 OpenClaw，由对方决定是否推送并回调 /mark-pushed。"""
    if not OPENCLAW_CALLBACK_URL:
        logger.warning("OPENCLAW_CALLBACK_URL not set")
        return
    if _is_openclaw_hooks_agent_url(OPENCLAW_CALLBACK_URL):
        callback_instruction = (
            f"若决定推送，请向以下 URL 发送 POST 请求：{YUQUE2GIT_PUBLIC_URL.rstrip('/')}/mark-pushed，"
            f"Body JSON：{{\"yuque_id\": {yuque_id}, \"commit\": \"{commit}\"}}。"
            if YUQUE2GIT_PUBLIC_URL
            else "若决定推送，请向部署本 yuque2git 服务的 /mark-pushed 发起 POST，Body JSON 含 yuque_id 与 commit。"
        )
        message = (
            f"【yuque2git 推送判定】\n\n"
            f"文档标题：{title}\n知识库：{repo_name}\n仓库：{repo_slug}\n文档：{doc_slug}\n\n"
            f"请根据以下 diff 判断是否推送到远程。{callback_instruction}\n\n---\n\nDiff:\n{diff}"
        )
        body = {"message": message, "name": "yuque2git", "deliver": False}
        headers = {}
        if OPENCLAW_HOOKS_TOKEN:
            headers["Authorization"] = f"Bearer {OPENCLAW_HOOKS_TOKEN}"
        else:
            logger.warning("OPENCLAW_HOOKS_TOKEN not set, Gateway may return 401")
    else:
        body = {
            "yuque_id": yuque_id,
            "repo_slug": repo_slug,
            "doc_slug": doc_slug,
            "title": title,
            "repo_name": repo_name,
            "diff": diff,
            "commit": commit,
            "action": "should_push",
        }
        headers = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(OPENCLAW_CALLBACK_URL, json=body, headers=headers or None)
    except Exception as e:
        logger.warning("OpenClaw callback POST failed: %s", e)


# --- FastAPI ---
app = FastAPI(title="yuque2git Webhook")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/mark-pushed")
async def mark_pushed(body: MarkPushedBody):
    if not OUTPUT_DIR or not OUTPUT_DIR.is_dir():
        return Response(status_code=500, content="OUTPUT_DIR not configured or missing")
    if body.commit:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", body.commit],
            cwd=OUTPUT_DIR,
            capture_output=True,
        )
        if r.returncode != 0:
            return Response(status_code=400, content="commit not found in repo")
    data = _read_last_push(OUTPUT_DIR)
    data[str(body.yuque_id)] = body.commit
    _write_last_push(OUTPUT_DIR, data)
    return {"ok": True, "yuque_id": body.yuque_id, "commit": body.commit}


@app.post("/webhook")
@app.post("/yuque")
async def webhook(request: Request):
    global OUTPUT_DIR
    if not OUTPUT_DIR or not OUTPUT_DIR.is_dir():
        return Response(status_code=500, content="OUTPUT_DIR not configured or missing")
    if not YUQUE_TOKEN:
        return Response(status_code=500, content="YUQUE_TOKEN not set")

    try:
        raw = await request.json()
        payload = WebhookPayload(**raw)
    except Exception as e:
        logger.warning("Invalid webhook body: %s", e)
        return Response(status_code=400, content="Invalid payload")

    data = payload.data
    action = data.action_type
    logger.info("Webhook: %s (id=%s)", action, data.id)

    if action in ("publish", "update"):
        if not data.book:
            logger.error("Doc event missing book")
            return Response(status_code=200, content="ok")  # 避免语雀重试

        repo_id = data.book.id
        repo_slug = data.book.slug
        slug = data.slug
        client = YuqueClient()
        try:
            toc_list = await client.get_repo_toc(repo_id)
        except Exception as e:
            logger.warning("get_repo_toc failed for repo_id=%s: %s", repo_id, e)
            return Response(status_code=200, content="ok")  # 避免语雀反复重试

        if not slug:
            for n in toc_list:
                if n.get("id") == data.id:
                    slug = n.get("url") or n.get("slug") or ""
                    break
            if not slug:
                logger.warning("Cannot resolve slug for doc id=%s", data.id)
                return Response(status_code=200, content="ok")

        try:
            detail = await client.get_doc_detail(repo_id, slug)
        except Exception as e:
            logger.warning("get_doc_detail failed for repo=%s slug=%s: %s", repo_id, slug, e)
            return Response(status_code=200, content="ok")  # 避免语雀反复重试
        if not detail:
            logger.warning("get_doc_detail failed for repo=%s slug=%s", repo_id, slug)
            return Response(status_code=200, content="ok")

        parent_path = _parent_path_from_toc(toc_list, data.id, slug)
        repo_dir = OUTPUT_DIR / _slug_safe(repo_slug)
        if parent_path:
            out_file = repo_dir / parent_path / (_slug_safe(slug) + ".md")
        else:
            out_file = repo_dir / (_slug_safe(slug) + ".md")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        rel_path = str(out_file.relative_to(OUTPUT_DIR))
        _ensure_git(OUTPUT_DIR)
        index = _read_index(OUTPUT_DIR)
        old_path = index.get(str(data.id))
        if old_path and old_path != rel_path:
            old_full = OUTPUT_DIR / old_path
            if old_full.exists():
                old_full.unlink()
                _git_add_commit(OUTPUT_DIR, [old_path], f"yuque: remove old path (move) id={data.id}")
        author_name = (data.actor.name if data.actor else "") or ""
        content = _build_md(detail, author_name)
        out_file.write_text(content, encoding="utf-8")
        index[str(data.id)] = rel_path
        _write_index(OUTPUT_DIR, index)
        commit_hash = _git_add_commit(
            OUTPUT_DIR,
            [rel_path, IDX_FILE],
            f"yuque: {action} {detail.get('title', slug)}",
        )
        if commit_hash:
            logger.info("Committed %s -> %s", out_file.name, commit_hash[:7])

        # 智能推送：diff + LLM 或 OpenClaw
        last_push = _read_last_push(OUTPUT_DIR)
        base_commit = last_push.get(str(data.id))
        diff_text = _get_diff(
            OUTPUT_DIR, base_commit, rel_path, yuque_id=data.id, body_only=ENABLE_BODY_ONLY_DIFF
        )

        if PUSH_DECISION_MODE == "openclaw":
            await _openclaw_callback(
                yuque_id=data.id,
                repo_slug=repo_slug,
                doc_slug=slug or "",
                title=detail.get("title", "") or "",
                repo_name=data.book.name or "",
                diff=diff_text,
                commit=commit_hash or "",
            )
            return Response(status_code=200, content="ok")

        if PUSH_DECISION_MODE == "llm":
            if diff_text.strip() in ("[无文本变更]", "[文档移动，内容无变更]"):
                logger.info("Push decision: skip (no content change), no LLM call")
                should_push, update_summary = False, None
            else:
                should_push, update_summary = await _llm_should_push(
                    diff_text,
                    detail.get("title", "") or slug,
                    data.book.name or "",
                )
            if should_push and commit_hash:
                _update_last_push_for(OUTPUT_DIR, data.id, commit_hash)
                if GIT_PUSH_ON_PUSH:
                    subprocess.run(["git", "push"], cwd=OUTPUT_DIR, capture_output=True)
                await _notify_push(
                    yuque_id=data.id,
                    repo_slug=repo_slug,
                    doc_slug=slug or "",
                    title=detail.get("title", "") or "",
                    repo_name=data.book.name or "",
                    commit=commit_hash,
                    update_summary=update_summary,
                )
                await _send_email_push(
                    title=detail.get("title", "") or "",
                    repo_name=data.book.name or "",
                    repo_slug=repo_slug,
                    doc_slug=slug or "",
                    update_summary=update_summary,
                    commit=commit_hash,
                )
                logger.info("Push decision: yes, notified (summary=%s)", bool(update_summary))
            else:
                logger.info("Push decision: no (or no commit)")
        return Response(status_code=200, content="ok")

    elif action == "delete":
        repo_slug = (data.book.slug if data.book else None) or ""
        if repo_slug:
            doc_path = _find_doc_path_by_yuque_id(OUTPUT_DIR, repo_slug, data.id)
        else:
            for d in OUTPUT_DIR.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    doc_path = _find_doc_path_by_yuque_id(OUTPUT_DIR, d.name, data.id)
                    if doc_path:
                        break
            else:
                doc_path = None
        if doc_path and doc_path.exists():
            rel_del = str(doc_path.relative_to(OUTPUT_DIR))
            doc_path.unlink()
            _ensure_git(OUTPUT_DIR)
            index = _read_index(OUTPUT_DIR)
            index.pop(str(data.id), None)
            _write_index(OUTPUT_DIR, index)
            _git_add_commit(OUTPUT_DIR, [rel_del, IDX_FILE], f"yuque: delete doc id={data.id}")
            last = _read_last_push(OUTPUT_DIR)
            last.pop(str(data.id), None)
            _write_last_push(OUTPUT_DIR, last)
        return Response(status_code=200, content="ok")

    return Response(status_code=200, content="ok")


def main():
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description="yuque2git Webhook server")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=os.environ.get("OUTPUT_DIR"),
        help="文档存储目录（Git 仓库根），语雀文档将写入此目录下 {repo_slug}/...；可设环境变量 OUTPUT_DIR",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", type=str, default="0.0.0.0")
    args = parser.parse_args()
    if not args.output_dir:
        parser.error("请指定 --output-dir 或设置环境变量 OUTPUT_DIR")
    OUTPUT_DIR = Path(args.output_dir).resolve()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host=args.bind, port=args.port)


if __name__ == "__main__":
    main()
