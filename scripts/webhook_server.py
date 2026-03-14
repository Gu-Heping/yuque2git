#!/usr/bin/env python3
"""
yuque2git Webhook 服务：接收语雀 Webhook，写本地 Markdown + Git commit，支持智能推送（LLM / OpenClaw）。
"""
import argparse
import json
import logging
import os
import subprocess
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
NOTIFY_URL = os.environ.get("NOTIFY_URL", "").strip()
LAST_PUSH_FILE = ".yuque-last-push.json"  # key: yuque_id (str), value: commit hash


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


# --- FastAPI ---
app = FastAPI(title="yuque2git Webhook")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/mark-pushed")
async def mark_pushed(body: MarkPushedBody):
    if not OUTPUT_DIR or not OUTPUT_DIR.is_dir():
        return Response(status_code=500, content="OUTPUT_DIR not configured or missing")
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
        if not slug:
            toc_list = await YuqueClient().get_repo_toc(repo_id)
            for n in toc_list:
                if n.get("id") == data.id:
                    slug = n.get("url") or n.get("slug") or ""
                    break
            if not slug:
                logger.warning("Cannot resolve slug for doc id=%s", data.id)
                return Response(status_code=200, content="ok")

        detail = await YuqueClient().get_doc_detail(repo_id, slug)
        if not detail:
            logger.warning("get_doc_detail failed for repo=%s slug=%s", repo_id, slug)
            return Response(status_code=200, content="ok")

        toc_list = await YuqueClient().get_repo_toc(repo_id)
        parent_path = _parent_path_from_toc(toc_list, data.id, slug)
        repo_dir = OUTPUT_DIR / _slug_safe(repo_slug)
        if parent_path:
            out_file = repo_dir / parent_path / (_slug_safe(slug) + ".md")
        else:
            out_file = repo_dir / (_slug_safe(slug) + ".md")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        author_name = (data.actor.name if data.actor else "") or ""
        content = _build_md(detail, author_name)
        out_file.write_text(content, encoding="utf-8")

        _ensure_git(OUTPUT_DIR)
        commit_hash = _git_add_commit(
            OUTPUT_DIR,
            [str(out_file.relative_to(OUTPUT_DIR))],
            f"yuque: {action} {detail.get('title', slug)}",
        )
        if commit_hash:
            logger.info("Committed %s -> %s", out_file.name, commit_hash[:7])
        # TODO: diff + 智能推送（LLM / OpenClaw）
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
            doc_path.unlink()
            _ensure_git(OUTPUT_DIR)
            _git_add_commit(OUTPUT_DIR, [str(doc_path.relative_to(OUTPUT_DIR))], f"yuque: delete doc id={data.id}")
            last = _read_last_push(OUTPUT_DIR)
            last.pop(str(data.id), None)
            _write_last_push(OUTPUT_DIR, last)
        return Response(status_code=200, content="ok")

    return Response(status_code=200, content="ok")


def main():
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description="yuque2git Webhook server")
    parser.add_argument("--output-dir", type=Path, default=os.environ.get("OUTPUT_DIR"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", type=str, default="0.0.0.0")
    args = parser.parse_args()
    if not args.output_dir:
        parser.error("--output-dir or OUTPUT_DIR required")
    OUTPUT_DIR = args.output_dir.resolve()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host=args.bind, port=args.port)


if __name__ == "__main__":
    main()
