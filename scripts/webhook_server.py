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
import re
import smtplib
import subprocess
import zlib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from string import Formatter
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

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
YUQUE2GIT_DELIVER_CHANNEL = os.environ.get("YUQUE2GIT_DELIVER_CHANNEL", "").strip()
YUQUE2GIT_DELIVER_TO = os.environ.get("YUQUE2GIT_DELIVER_TO", "").strip()
# 多目标：JSON 数组 [{"channel":"qq","to":"id1"},...]；未设时由 CHANNEL+TO 解析（TO 可逗号分隔多个）
YUQUE2GIT_DELIVER_TARGETS_JSON = os.environ.get("YUQUE2GIT_DELIVER_TARGETS", "").strip()
# 多目标时两次 POST 之间的间隔（秒），避免连续请求触发 Gateway/上游 rate limit，默认 2
YUQUE2GIT_DELIVER_DELAY_SECONDS = max(0.0, float(os.environ.get("YUQUE2GIT_DELIVER_DELAY_SECONDS", "2.0")))
# OpenClaw/Gateway 返回 429 时的最大重试次数（每次指数退避），默认 3
YUQUE2GIT_DELIVER_MAX_RETRIES = max(0, int(os.environ.get("YUQUE2GIT_DELIVER_MAX_RETRIES", "3")))
YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE = os.environ.get("YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE", "").strip()
# 语雀文档原文地址：用于生成 doc_url，未设时从 detail.book.user.login 取
YUQUE_NAMESPACE = os.environ.get("YUQUE_NAMESPACE", "").strip()
# 元数据时间转为本地可读：YUQUE_TIMEZONE（如 Asia/Shanghai），未设默认 Asia/Shanghai
YUQUE_TIMEZONE = os.environ.get("YUQUE_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
NOTIFY_URL = os.environ.get("NOTIFY_URL", "").strip()
LAST_PUSH_FILE = ".yuque-last-push.json"  # key: yuque_id (str), value: commit hash
IDX_FILE = ".yuque-id-to-path.json"  # key: yuque_id (str), value: rel_path，用于文档移动时 diff 查旧路径
MEMBERS_FILE = ".yuque-members.json"  # key: user_id (str), value: {"name": "...", "login": "..."}，本地缓存避免重复请求
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
    should_push: bool = True
    repo_slug: Optional[str] = None
    doc_slug: Optional[str] = None
    summary: Optional[Dict[str, Any]] = None


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

    async def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        """GET /users/:id，返回语雀用户信息（含 name/login），用于团队内姓名。"""
        async with httpx.AsyncClient(headers=self.headers, timeout=15.0) as client:
            r = await client.get(f"{self.base_url}/users/{user_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json().get("data", {})


def _slug_safe(s: str) -> str:
    for c in r'/\:*?"<>|':
        s = s.replace(c, "_")
    return s.strip() or "untitled"


def _doc_basename(title: Optional[str], slug: str) -> str:
    """文档文件名（不含 .md）：使用标题，无标题时用 slug。"""
    return _slug_safe(title or slug) or "untitled"


def _yuque_id_from_md(file_path: Path) -> Optional[int]:
    """从已有 .md 的 frontmatter 读取 id/yuque_id，无法解析时返回 None。"""
    if not file_path.exists() or not file_path.is_file():
        return None
    try:
        text = file_path.read_text(encoding="utf-8")
        if not text.strip().startswith("---"):
            return None
        parts = text.split("---", 2)
        if len(parts) < 2:
            return None
        fm = yaml.safe_load(parts[1])
        if not fm:
            return None
        raw = fm.get("id") or fm.get("yuque_id")
        if raw is None:
            return None
        return int(raw) if isinstance(raw, int) else int(raw) if isinstance(raw, str) and raw.isdigit() else None
    except Exception:
        return None


def _resolve_doc_basename(parent_dir: Path, base: str, yuque_id: Optional[int]) -> str:
    """在父目录下解析最终文件名：优先 base.md；若已存在且属其他文档则用 base_2.md、base_3.md 等。"""
    candidate = base + ".md"
    path = parent_dir / candidate
    if not path.exists():
        return candidate
    existing_id = _yuque_id_from_md(path)
    if existing_id == yuque_id:
        return candidate
    for i in range(2, 100):
        candidate = f"{base}_{i}.md"
        path = parent_dir / candidate
        if not path.exists():
            return candidate
        if _yuque_id_from_md(path) == yuque_id:
            return candidate
    return f"{base}_2.md"


def _parent_path_from_toc(toc_list: List[Dict], doc_id: int, doc_slug: Optional[str]) -> str:
    """根据 TOC 和文档 id/slug 得到父路径（用于目录层级），根层返回 ''。
    与 sync_to_files 一致：目录段使用 title（便于识别），无 title 时用 uuid。"""
    by_uuid: Dict[str, Dict] = {n["uuid"]: n for n in toc_list if n.get("uuid")}
    by_id: Dict[int, Dict] = {}
    for n in toc_list:
        i = n.get("id")
        if i is not None and (isinstance(i, int) or (isinstance(i, str) and i.isdigit())):
            by_id[int(i)] = n

    def segment_name(node: Dict) -> str:
        """与 sync_to_files 一致：目录名用 title，无则用 uuid。"""
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

    parts = [segment_name(p) for p in ancestors(target)]
    return "/".join(parts) if parts else ""


# 仅写入 Obsidian 友好 + 同步必需的少量属性，避免冗余（type/body_*/book 嵌套/统计/多时间戳等）


def _author_name_from_detail(detail: Dict[str, Any]) -> str:
    """从文档详情 API 返回的 last_editor / creator / user 中取显示名（webhook 常不带 actor）。"""
    for key in ("last_editor", "creator", "user"):
        obj = detail.get(key)
        if isinstance(obj, dict):
            name = (obj.get("name") or obj.get("login") or "").strip()
            if name:
                return name
    return ""


async def _resolve_author_name(
    client: "YuqueClient",
    output_dir: Path,
    detail: Dict[str, Any],
    actor_name: str = "",
) -> str:
    """解析作者显示名：优先 actor，再 .yuque-members.json（团队内姓名），再 detail 内嵌，缺失时 GET /users/:id。"""
    if (actor_name or "").strip():
        return (actor_name or "").strip()
    user_id = detail.get("last_editor_id") or detail.get("user_id")
    user_id_str = str(user_id) if user_id else ""
    members = _read_members(output_dir)
    if user_id_str and user_id_str in members:
        return (members[user_id_str].get("name") or members[user_id_str].get("login") or "").strip()
    name = _author_name_from_detail(detail)
    if name:
        return name
    if not user_id:
        return ""
    try:
        user_data = await client.get_user_by_id(user_id)
        if user_data:
            entry = {
                "name": (user_data.get("name") or "").strip(),
                "login": (user_data.get("login") or "").strip(),
            }
            members[user_id_str] = entry
            _write_members(output_dir, members)
            return (entry["name"] or entry["login"] or "").strip()
    except Exception as e:
        logger.warning("get_user_by_id %s failed: %s", user_id, e)
    return ""


def _normalize_ts_local(ts: Optional[str]) -> str:
    """语雀返回 UTC，转为本地可读时间 YYYY-MM-DD HH:MM:SS（时区见 YUQUE_TIMEZONE）。"""
    if not ts or not isinstance(ts, str):
        return str(ts) if ts else ""
    t = ts.strip().replace("Z", "+00:00")
    if "T" not in t:
        return t
    t = re.sub(r"\.\d+", "", t)
    if not re.search(r"[+-]\d{2}:\d{2}$", t):
        t = t + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(ZoneInfo(YUQUE_TIMEZONE))
        return local.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return t


def _build_md(detail: Dict[str, Any], author_name: str = "") -> str:
    """frontmatter（仅允许字段）+ 元数据表格 + 正文。"""
    created = _normalize_ts_local(detail.get("created_at") or "")
    updated = _normalize_ts_local(detail.get("updated_at") or detail.get("content_updated_at") or "")

    fm = {}
    for k in ("id", "title", "slug"):
        if k in detail and detail[k] is not None:
            fm[k] = detail[k]
    fm["created_at"] = created
    fm["updated_at"] = updated
    if author_name:
        fm["author"] = author_name
    book = detail.get("book")
    if isinstance(book, dict) and (book.get("name") or "").strip():
        fm["book_name"] = (book.get("name") or "").strip()
    for k in ("description", "cover"):
        v = detail.get(k)
        if v and isinstance(v, str) and v.strip():
            fm[k] = v.strip()

    yaml_block = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
    table_cell = (lambda x: x.replace("|", "\\|").replace("\n", " ").strip() if x else "")
    author_display = table_cell(author_name or str(detail.get("user_id") or ""))

    md = "---\n" + yaml_block + "\n---\n\n"
    md += "| 作者 | 创建时间 | 更新时间 |\n|------|----------|----------|\n"
    md += f"| {author_display} | {table_cell(str(created))} | {table_cell(str(updated))} |\n\n"
    md += _render_doc_body(detail)
    if not md.endswith("\n"):
        md += "\n"
    return md


def _render_doc_body(detail: Dict[str, Any]) -> str:
    if (detail.get("format") or "").lower() == "lakesheet" or (detail.get("type") or "").lower() == "sheet":
        rendered = _render_lakesheet_markdown(detail.get("body") or "")
        if rendered:
            return rendered
    return (detail.get("body") or "").strip()


def _render_lakesheet_markdown(raw_body: Any) -> str:
    try:
        payload = raw_body if isinstance(raw_body, dict) else json.loads(raw_body or "{}")
    except (TypeError, json.JSONDecodeError):
        return str(raw_body or "").strip()
    compressed = payload.get("sheet")
    if not isinstance(compressed, str) or not compressed:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        decoded = zlib.decompress(compressed.encode("latin1")).decode("utf-8")
        sheets = json.loads(decoded)
    except Exception:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if not isinstance(sheets, list):
        return decoded.strip()

    blocks: List[str] = [
        "> Auto-generated from Yuque lakesheet for readable review and diff.",
        "",
    ]
    for idx, sheet in enumerate(sheets, start=1):
        if not isinstance(sheet, dict):
            continue
        title = (sheet.get("name") or f"Sheet{idx}").strip()
        blocks.append(f"## {title}")
        blocks.append("")
        blocks.append("```tsv")
        blocks.extend(_sheet_to_tsv_lines(sheet))
        blocks.append("```")
        blocks.append("")
    return "\n".join(blocks).strip()


def _sheet_to_tsv_lines(sheet: Dict[str, Any]) -> List[str]:
    data = sheet.get("data")
    if not isinstance(data, dict) or not data:
        return ["(empty)"]

    used_rows = set()
    used_cols = set()
    for row_key, row in data.items():
        if not isinstance(row, dict):
            continue
        try:
            row_idx = int(row_key)
        except (TypeError, ValueError):
            continue
        for col_key, cell in row.items():
            if not isinstance(cell, dict):
                continue
            try:
                col_idx = int(col_key)
            except (TypeError, ValueError):
                continue
            if _cell_has_content(cell):
                used_rows.add(row_idx)
                used_cols.add(col_idx)

    if not used_rows or not used_cols:
        return ["(empty)"]

    max_col = max(used_cols)
    lines: List[str] = []
    for row_idx in range(min(used_rows), max(used_rows) + 1):
        row = data.get(str(row_idx), {})
        rendered = [_cell_to_text(row.get(str(col_idx))) for col_idx in range(0, max_col + 1)]
        while rendered and rendered[-1] == "":
            rendered.pop()
        lines.append("\t".join(rendered))
    return lines or ["(empty)"]


def _cell_has_content(cell: Dict[str, Any]) -> bool:
    return _cell_to_text(cell) != ""


def _cell_to_text(cell: Any) -> str:
    if not isinstance(cell, dict):
        return ""
    for key in ("m", "v", "f"):
        value = cell.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            text = str(value)
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").strip()
        if text:
            return text.replace("\n", "\\n")
    return ""


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
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 and "nothing to commit" not in (result.stderr or "").lower():
        logger.warning("git commit: %s", result.stderr)
        return None
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=output_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
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


def _read_members(output_dir: Path) -> Dict[str, Dict[str, str]]:
    """user_id (str) -> { "name", "login" }"""
    p = output_dir / MEMBERS_FILE
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_members(output_dir: Path, data: Dict[str, Dict[str, str]]) -> None:
    p = output_dir / MEMBERS_FILE
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _find_doc_path_by_yuque_id(output_dir: Path, repo_slug: str, yuque_id: int) -> Optional[Path]:
    """在 repo_slug 对应目录下按 frontmatter 的 yuque_id 查找 .md 文件。"""
    repo_dir = output_dir / _slug_safe(repo_slug)
    if not repo_dir.is_dir():
        return None
    return _find_doc_in_dir_by_yuque_id(repo_dir, yuque_id)


def _find_doc_in_dir_by_yuque_id(repo_dir: Path, yuque_id: int) -> Optional[Path]:
    """在给定目录下按 frontmatter 的 id/yuque_id 查找 .md 文件。"""
    for md in repo_dir.rglob("*.md"):
        if md.name == ".md":
            continue
        try:
            raw = md.read_text(encoding="utf-8")
            if raw.startswith("---"):
                end = raw.index("---", 3) if "---" in raw[3:] else -1
                if end > 0:
                    fm = yaml.safe_load(raw[3:end])
                    if not fm:
                        continue
                    doc_id = fm.get("yuque_id") if fm.get("yuque_id") is not None else fm.get("id")
                    if doc_id == yuque_id:
                        return md
        except Exception:
            continue
    return None


def _find_doc_path_by_yuque_id_any_repo(output_dir: Path, yuque_id: int) -> Optional[Path]:
    """在所有顶层仓库目录下按 yuque_id 查找 .md，与「名称目录」或 slug 目录一致。"""
    for d in output_dir.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            found = _find_doc_in_dir_by_yuque_id(d, yuque_id)
            if found:
                return found
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
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
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
        encoding="utf-8",
        errors="replace",
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
        summary_instruction = (
            ' 同时生成 "update_summary" 字段，内容必须是 1～3 句可直接发给订阅者的中文摘要。'
            ' 只要 should_push=true，update_summary 就必须为非空，且要明确说明改了什么；'
            ' 只有 should_push=false 时才允许为空字符串。'
        )
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
{'- update_summary: string（1～3 句中文变更摘要；当 should_push=true 时必须非空，当 should_push=false 时可为空字符串）' if ENABLE_UPDATE_SUMMARY else ''}
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
        should_raw = obj.get("should_push", False)
        if not isinstance(should_raw, bool):
            logger.warning("LLM response should_push must be boolean, got %r", should_raw)
            return False, None
        should = should_raw
        summary = None
        if ENABLE_UPDATE_SUMMARY:
            summary_raw = obj.get("update_summary", "")
            if summary_raw is None:
                summary_raw = ""
            if not isinstance(summary_raw, str):
                logger.warning("LLM response update_summary must be string, got %r", type(summary_raw).__name__)
                return False, None
            summary = summary_raw.strip()
            if should and not summary:
                logger.warning("LLM response missing update_summary while should_push=true")
                return False, None
        return should, summary
    except json.JSONDecodeError as e:
        logger.warning("LLM response not valid JSON: %s", e)
        return False, None


def _build_openclaw_reply_contract(title: str, doc_url: str, deliver_targets: List[Dict[str, str]]) -> str:
    """统一约束 OpenClaw Agent 的回文格式，尽量提高摘要完整性与稳定性。"""
    link_placeholder = doc_url or "（未提供原文链接，需按此原样保留）"
    push_note = "若决定推送，必须先向 /mark-pushed 回调结构化 JSON；不要把摘要直接发给订阅者。"
    target_note = "服务端会在收到合法 JSON 后统一投递给订阅者。" if deliver_targets else "服务端会在收到合法 JSON 后统一生成摘要。"
    return (
        "若决定不推送，请回调 JSON："
        '{"yuque_id": <yuque_id>, "commit": "<commit>", "should_push": false}。\n'
        f"{push_note}{target_note}\n"
        "若决定推送，请回调 JSON，且只能回调这一种结构：\n"
        "{\n"
        f'  "yuque_id": <yuque_id>,\n  "commit": "<commit>",\n  "should_push": true,\n'
        '  "summary": {\n'
        f'    "title": "{title}",\n'
        '    "repo_name": "<知识库名>",\n'
        '    "author": "<作者名>",\n'
        f'    "doc_url": "{link_placeholder}",\n'
        '    "highlights": ["<变更要点1>", "<变更要点2，可省略>", "<变更要点3，可省略>"]\n'
        "  }\n"
        "}\n"
        "要求：\n"
        "- title、repo_name、author、doc_url 必须非空。\n"
        "- highlights 必须是 1～3 条字符串，每条都要写清楚具体改了什么。\n"
        "- 禁止返回额外说明、Markdown 代码块或自然语言正文。"
    )


def _render_openclaw_message_template(template: str, values: Dict[str, Any]) -> str:
    """渲染自定义模板；缺少字段时保守降级为报错，避免静默丢失关键信息。"""
    required_fields = {name for _, name, _, _ in Formatter().parse(template) if name}
    missing = sorted(name for name in required_fields if name not in values)
    if missing:
        raise ValueError(f"OpenClaw message template references unknown fields: {', '.join(missing)}")
    return template.format(**values)


def _validate_openclaw_summary(summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """校验 OpenClaw 回传的结构化摘要；不合法时返回 None。"""
    if not isinstance(summary, dict):
        return None
    title = str(summary.get("title") or "").strip()
    repo_name = str(summary.get("repo_name") or "").strip()
    author = str(summary.get("author") or "").strip()
    doc_url = str(summary.get("doc_url") or "").strip()
    highlights_raw = summary.get("highlights")
    if not title or not repo_name or not author or not doc_url:
        return None
    if not isinstance(highlights_raw, list):
        return None
    highlights = [str(item).strip() for item in highlights_raw if str(item).strip()]
    if not 1 <= len(highlights) <= 3:
        return None
    return {
        "title": title,
        "repo_name": repo_name,
        "author": author,
        "doc_url": doc_url,
        "highlights": highlights,
    }


def _format_openclaw_summary(summary: Dict[str, Any]) -> str:
    """将结构化摘要渲染为最终投递文案。"""
    lines = [
        f"【已更新】《{summary['title']}》",
        f"知识库：{summary['repo_name']}",
        f"作者：{summary['author']}",
        f"原文：{summary['doc_url']}",
        "摘要：",
    ]
    lines.extend(f"{idx}. {item}" for idx, item in enumerate(summary["highlights"], start=1))
    return "\n".join(lines)


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


def _parse_deliver_targets() -> List[Dict[str, str]]:
    """解析推送目标列表。支持 YUQUE2GIT_DELIVER_TARGETS JSON 数组，或 CHANNEL+TO（TO 可逗号分隔）。"""
    if YUQUE2GIT_DELIVER_TARGETS_JSON:
        try:
            raw = json.loads(YUQUE2GIT_DELIVER_TARGETS_JSON)
            if isinstance(raw, list):
                return [
                    {"channel": str(t.get("channel", "")).strip(), "to": str(t.get("to", "")).strip()}
                    for t in raw
                    if isinstance(t, dict) and (t.get("channel") or "").strip() and (t.get("to") or "").strip()
                ]
        except (json.JSONDecodeError, TypeError):
            pass
    ch = (YUQUE2GIT_DELIVER_CHANNEL or "").strip()
    to_raw = (YUQUE2GIT_DELIVER_TO or "").strip()
    if not ch or not to_raw:
        return []
    return [{"channel": ch, "to": t.strip()} for t in to_raw.split(",") if t.strip()]


def _is_openclaw_hooks_agent_url(url: str) -> bool:
    """是否指向 OpenClaw Gateway 的 /hooks/agent 入口。"""
    u = (url or "").rstrip("/")
    return u.endswith("/hooks/agent")


def _doc_url(namespace: str, repo_slug: str, doc_slug: str) -> str:
    """语雀文档原文地址。base 从 YUQUE_BASE_URL 推导（如 nova.yuque.com）。"""
    base = YUQUE_BASE_URL.replace("/api/v2", "").rstrip("/") or "https://nova.yuque.com"
    if not namespace or not repo_slug or not doc_slug:
        return ""
    return f"{base}/{namespace}/{repo_slug}/{doc_slug}"


async def _openclaw_callback(
    yuque_id: int,
    repo_slug: str,
    doc_slug: str,
    title: str,
    repo_name: str,
    diff: str,
    commit: str,
    author_name: str = "",
    doc_url: str = "",
    local_path: str = "",
) -> None:
    """将「待判定」事件 POST 到 OpenClaw，由对方决定是否推送并回调 /mark-pushed。"""
    if not OPENCLAW_CALLBACK_URL:
        logger.warning("OPENCLAW_CALLBACK_URL not set")
        return
    # 供 Agent 直接读取的本地绝对路径（与 OUTPUT_DIR 同机时可用 read 工具）
    local_path_abs = str(OUTPUT_DIR / local_path) if (OUTPUT_DIR and local_path) else local_path
    if _is_openclaw_hooks_agent_url(OPENCLAW_CALLBACK_URL):
        deliver_targets = _parse_deliver_targets()
        callback_instruction = (
            f"请向以下 URL 发送 POST 请求：{YUQUE2GIT_PUBLIC_URL.rstrip('/')}/mark-pushed，"
            f"Body JSON 必须包含 yuque_id、commit、should_push；若 should_push=true，还必须携带 summary。"
            if YUQUE2GIT_PUBLIC_URL
            else "请向部署本 yuque2git 服务的 /mark-pushed 发起 POST，Body JSON 必须包含 yuque_id、commit、should_push；若 should_push=true，还必须携带 summary。"
        )
        reply_contract = _build_openclaw_reply_contract(title, doc_url, deliver_targets)
        template_values = {
            "title": title,
            "repo_name": repo_name,
            "repo_slug": repo_slug,
            "doc_slug": doc_slug,
            "diff": diff,
            "yuque_id": yuque_id,
            "commit": commit,
            "callback_instruction": callback_instruction,
            "author": author_name,
            "doc_url": doc_url,
            "local_path": local_path_abs or local_path,
            "reply_contract": reply_contract,
        }
        if YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE:
            try:
                message = _render_openclaw_message_template(YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE, template_values)
            except ValueError as e:
                logger.warning("%s", e)
                return
        else:
            author_line = f"作者：{author_name}\n" if author_name else ""
            doc_url_line = f"原文地址：{doc_url or '（未提供原文链接）'}\n"
            local_path_line = ""
            if local_path_abs:
                local_path_line = f"本地文件（可直接读取以生成概要）：{local_path_abs}\n"
            message = (
                f"【yuque2git 推送判定】\n\n"
                f"文档标题：{title}\n知识库：{repo_name}\n仓库：{repo_slug}\n文档：{doc_slug}\n"
                f"{author_line}{doc_url_line}{local_path_line}\n"
                f"请根据以下 diff 判断是否推送到远程。{callback_instruction}\n"
                f"{reply_contract}\n\n---\n\nDiff:\n{diff}"
            )
        headers = {}
        if OPENCLAW_HOOKS_TOKEN:
            headers["Authorization"] = f"Bearer {OPENCLAW_HOOKS_TOKEN}"
        else:
            logger.warning("OPENCLAW_HOOKS_TOKEN not set, Gateway may return 401")
        bodies = [{"message": message, "name": "yuque2git", "deliver": False}]
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
        bodies = [body]
    async def _post_with_retry(client: httpx.AsyncClient, b: Dict, channel: str, to: str) -> None:
        last_exc = None
        for attempt in range(YUQUE2GIT_DELIVER_MAX_RETRIES + 1):
            r = await client.post(OPENCLAW_CALLBACK_URL, json=b, headers=headers or None)
            if r.status_code == 429 and attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
                wait = 2 ** attempt
                if "Retry-After" in r.headers:
                    try:
                        wait = int(r.headers["Retry-After"])
                    except ValueError:
                        pass
                logger.warning(
                    "OpenClaw deliver 429 (target %s/%s), retry after %ss (attempt %s/%s)",
                    channel, to, wait, attempt + 1, YUQUE2GIT_DELIVER_MAX_RETRIES + 1,
                )
                await asyncio.sleep(wait)
                last_exc = httpx.HTTPStatusError("429 Too Many Requests", request=r.request, response=r)
                continue
            if r.status_code >= 400:
                logger.warning("OpenClaw POST target %s/%s returned %s: %s", channel, to, r.status_code, (r.text or "")[:200])
            return
        if last_exc:
            logger.warning("OpenClaw deliver failed after retries: %s", last_exc)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for i, b in enumerate(bodies):
                if i > 0 and YUQUE2GIT_DELIVER_DELAY_SECONDS > 0 and len(bodies) > 1:
                    await asyncio.sleep(YUQUE2GIT_DELIVER_DELAY_SECONDS)
                channel = b.get("channel", "")
                to = b.get("to", "")
                if channel or to:
                    logger.info("OpenClaw deliver target %s/%s (request %d/%d)", channel, to, i + 1, len(bodies))
                await _post_with_retry(client, b, channel, to)
    except Exception as e:
        logger.warning("OpenClaw callback POST failed: %s", e)


async def _deliver_openclaw_summary(summary: Dict[str, Any]) -> None:
    """将服务端拼装好的摘要通过 Gateway 投递给配置目标。"""
    if not _is_openclaw_hooks_agent_url(OPENCLAW_CALLBACK_URL):
        return
    deliver_targets = _parse_deliver_targets()
    if not deliver_targets:
        return
    message = _format_openclaw_summary(summary)
    headers = {}
    if OPENCLAW_HOOKS_TOKEN:
        headers["Authorization"] = f"Bearer {OPENCLAW_HOOKS_TOKEN}"
    bodies = [
        {"message": message, "name": "yuque2git", "deliver": True, "channel": t["channel"], "to": t["to"]}
        for t in deliver_targets
    ]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for i, body in enumerate(bodies):
                if i > 0 and YUQUE2GIT_DELIVER_DELAY_SECONDS > 0 and len(bodies) > 1:
                    await asyncio.sleep(YUQUE2GIT_DELIVER_DELAY_SECONDS)
                await client.post(OPENCLAW_CALLBACK_URL, json=body, headers=headers or None)
    except Exception as e:
        logger.warning("OpenClaw summary deliver failed: %s", e)


# --- FastAPI ---
app = FastAPI(title="yuque2git Webhook")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/mark-pushed")
async def mark_pushed(body: MarkPushedBody):
    if not OUTPUT_DIR or not OUTPUT_DIR.is_dir():
        return Response(status_code=500, content="OUTPUT_DIR not configured or missing")
    if body.should_push and body.commit:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", body.commit],
            cwd=OUTPUT_DIR,
            capture_output=True,
        )
        if r.returncode != 0:
            return Response(status_code=400, content="commit not found in repo")
    validated_summary = _validate_openclaw_summary(body.summary) if body.should_push else None
    if body.should_push and not validated_summary:
        return Response(status_code=400, content="summary missing or invalid")
    if body.should_push:
        data = _read_last_push(OUTPUT_DIR)
        data[str(body.yuque_id)] = body.commit
        _write_last_push(OUTPUT_DIR, data)
        await _deliver_openclaw_summary(validated_summary)
        return {"ok": True, "yuque_id": body.yuque_id, "commit": body.commit, "delivered": True}
    return {"ok": True, "yuque_id": body.yuque_id, "commit": body.commit, "delivered": False}


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
        # 与全量同步一致：目录名用知识库名称（便于识别）
        repo_dir_name = _slug_safe(data.book.name or data.book.slug or "")
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
        repo_dir = OUTPUT_DIR / repo_dir_name
        base = _doc_basename(detail.get("title"), slug or "")
        parent_dir = repo_dir / parent_path if parent_path else repo_dir
        parent_dir.mkdir(parents=True, exist_ok=True)
        doc_basename = _resolve_doc_basename(parent_dir, base, data.id)
        if parent_path:
            out_file = repo_dir / parent_path / doc_basename
        else:
            out_file = repo_dir / doc_basename
        rel_path = out_file.relative_to(OUTPUT_DIR).as_posix()
        _ensure_git(OUTPUT_DIR)
        index = _read_index(OUTPUT_DIR)
        old_path = index.get(str(data.id))
        if old_path and old_path != rel_path:
            old_full = OUTPUT_DIR / old_path
            if old_full.exists():
                old_full.unlink()
                _git_add_commit(OUTPUT_DIR, [old_path], f"yuque: remove old path (move) id={data.id}")
        author_name = await _resolve_author_name(
            client, OUTPUT_DIR, detail, (data.actor.name if data.actor else "")
        )
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

        # 正文无变化时不触发 OpenClaw 分析
        if diff_text.strip() in ("[无文本变更]", "[文档移动，内容无变更]"):
            logger.info("Body unchanged, skip push decision (no OpenClaw/LLM)")
            return Response(status_code=200, content="ok")

        if PUSH_DECISION_MODE == "openclaw":
            namespace = (
                (detail.get("book") or {}).get("user") or {}
            ).get("login") or YUQUE_NAMESPACE
            doc_link = _doc_url(namespace, repo_slug, slug or "") if (repo_slug and slug) else ""
            await _openclaw_callback(
                yuque_id=data.id,
                repo_slug=repo_slug,
                doc_slug=slug or "",
                title=detail.get("title", "") or "",
                repo_name=data.book.name or "",
                diff=diff_text,
                commit=commit_hash or "",
                author_name=author_name,
                doc_url=doc_link,
                local_path=rel_path,
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
        # 文档可能写在「知识库名称」或 slug 目录下，统一按 yuque_id 在所有仓库下查找
        doc_path = _find_doc_path_by_yuque_id_any_repo(OUTPUT_DIR, data.id)
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
