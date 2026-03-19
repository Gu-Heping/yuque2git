#!/usr/bin/env python3
"""
yuque2git 全量同步：拉取用户知识库列表与 TOC，将 type=DOC 的文档写成 Markdown，按 TOC 层级建目录，写入 .toc.json 与 .repos.json。
含限流：可配置并发数、请求间隔、429/5xx 重试与退避。
"""
import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
import yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

YUQUE_TOKEN = os.environ.get("YUQUE_TOKEN", "")
YUQUE_BASE_URL = os.environ.get("YUQUE_BASE_URL", "https://nova.yuque.com/api/v2").rstrip("/")
YUQUE_TIMEZONE = (os.environ.get("YUQUE_TIMEZONE", "Asia/Shanghai") or "Asia/Shanghai").strip()
# 限流与重试
YUQUE_SYNC_CONCURRENCY = int(os.environ.get("YUQUE_SYNC_CONCURRENCY", "3"))
YUQUE_SYNC_REQUEST_DELAY = float(os.environ.get("YUQUE_SYNC_REQUEST_DELAY", "0.25"))
YUQUE_SYNC_MAX_RETRIES = int(os.environ.get("YUQUE_SYNC_MAX_RETRIES", "4"))
SEMAPHORE = asyncio.Semaphore(YUQUE_SYNC_CONCURRENCY)


def _slug_safe(s: str) -> str:
    for c in r'/\:*?"<>|':
        s = s.replace(c, "_")
    return s.strip() or "untitled"


def _doc_basename(title: Optional[str], slug: str) -> str:
    """与 webhook 一致：文档文件名用标题，无标题时用 slug。"""
    return _slug_safe(title or slug) or "untitled"


def _parse_time(ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    return ts.replace("Z", "+00:00") if isinstance(ts, str) else None


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


def _author_name_from_detail(detail: Dict[str, Any]) -> str:
    """从文档详情的 last_editor / creator / user 中取显示名。"""
    for key in ("last_editor", "creator", "user"):
        obj = detail.get(key)
        if isinstance(obj, dict):
            name = (obj.get("name") or obj.get("login") or "").strip()
            if name:
                return name
    return ""


def _read_members(output_dir: Path) -> Dict[str, Dict[str, str]]:
    """读取 .yuque-members.json，全量同步时优先用团队内姓名。"""
    p = output_dir / ".yuque-members.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_members(output_dir: Path, data: Dict[str, Dict[str, str]]) -> None:
    """写入 .yuque-members.json，与 webhook 格式一致。"""
    p = output_dir / ".yuque-members.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
) -> httpx.Response:
    """带重试的请求：429、5xx、连接错误时指数退避重试，尊重 Retry-After。"""
    last_exc = None
    for attempt in range(YUQUE_SYNC_MAX_RETRIES):
        try:
            r = await client.request(method, url)
            if r.status_code == 429:
                wait = 2 ** attempt
                if "Retry-After" in r.headers:
                    try:
                        wait = int(r.headers["Retry-After"])
                    except ValueError:
                        pass
                logger.warning("Rate limited (429), retry after %ss (attempt %s/%s)", wait, attempt + 1, YUQUE_SYNC_MAX_RETRIES)
                await asyncio.sleep(wait)
                last_exc = httpx.HTTPStatusError("429 Too Many Requests", request=r.request, response=r)
                continue
            if 500 <= r.status_code < 600:
                wait = 2 ** attempt
                logger.warning("Server error %s, retry after %ss (attempt %s/%s)", r.status_code, wait, attempt + 1, YUQUE_SYNC_MAX_RETRIES)
                await asyncio.sleep(wait)
                last_exc = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                continue
            return r
        except (httpx.RequestError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            wait = 2 ** attempt
            logger.warning("Request error %s, retry after %ss (attempt %s/%s)", e, wait, attempt + 1, YUQUE_SYNC_MAX_RETRIES)
            await asyncio.sleep(wait)
            last_exc = e
    if last_exc:
        raise last_exc
    raise RuntimeError("Unexpected retry loop exit")


class YuqueClient:
    def __init__(self):
        self.headers = {
            "X-Auth-Token": YUQUE_TOKEN,
            "User-Agent": "yuque2git/1.0",
            "Content-Type": "application/json",
        }

    async def _get(self, path: str) -> httpx.Response:
        url = f"{YUQUE_BASE_URL}{path}"
        async with SEMAPHORE:
            async with httpx.AsyncClient(headers=self.headers, timeout=30.0) as client:
                r = await _request_with_retry(client, "GET", url)
            if YUQUE_SYNC_REQUEST_DELAY > 0:
                await asyncio.sleep(YUQUE_SYNC_REQUEST_DELAY)
        return r

    async def get_user(self) -> Optional[Dict]:
        r = await self._get("/user")
        r.raise_for_status()
        return r.json().get("data")

    async def get_user_repos(self, user_id: int) -> List[Dict]:
        r = await self._get(f"/users/{user_id}/repos")
        r.raise_for_status()
        return r.json().get("data", [])

    async def get_repo_toc(self, repo_id: int) -> List[Dict]:
        r = await self._get(f"/repos/{repo_id}/toc")
        r.raise_for_status()
        return r.json().get("data", [])

    async def get_doc_detail(self, repo_id: int, slug: str) -> Optional[Dict]:
        r = await self._get(f"/repos/{repo_id}/docs/{slug}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("data", {})

    async def get_group_members_page(self, group_id: int, page: int) -> List[Dict]:
        """GET /groups/{id}/statistics/members 分页，与 yuque-sync-platform 一致。404（个人账号非团队）返回空列表。"""
        path = f"/groups/{group_id}/statistics/members?page={page}"
        r = await self._get(path)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json().get("data", {}).get("members", [])


async def fetch_and_write_team_members(client: YuqueClient, group_id: int, output_dir: Path) -> None:
    """全量同步前拉取团队成员并写入 .yuque-members.json（参考 yuque-sync-platform sync_team_members）。"""
    members = _read_members(output_dir)
    page = 1
    fetched_any = False
    while True:
        try:
            raw = await client.get_group_members_page(group_id, page)
        except Exception as e:
            logger.warning("fetch team members page %s failed (e.g. personal account): %s", page, e)
            break
        if not raw:
            break
        fetched_any = True
        for item in raw:
            user_info = item.get("user") or {}
            uid = user_info.get("id") or item.get("user_id")
            if not uid:
                continue
            uid_str = str(uid)
            members[uid_str] = {
                "name": (user_info.get("name") or "Unknown").strip(),
                "login": (user_info.get("login") or f"u_{uid}").strip(),
            }
        page += 1
        if YUQUE_SYNC_REQUEST_DELAY > 0:
            await asyncio.sleep(0.2)
    if fetched_any:
        _write_members(output_dir, members)
        logger.info("Wrote .yuque-members.json with %s members", len(members))


def _build_md(detail: Dict[str, Any], author_name: str = "") -> str:
    """frontmatter 与 webhook_server 一致：仅 id/title/slug/created_at/updated_at/author/book_name/description/cover；时间为本地可读。"""
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
    if not md.rstrip():
        pass
    elif not md.endswith("\n"):
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


def _resolve_doc_basename(used_bases: Dict[tuple, set], repo_dir_name: str, parent_path: str, base: str) -> str:
    """同目录下重名时用 base_2.md、base_3.md，与 webhook 一致。"""
    key = (repo_dir_name, parent_path)
    used = used_bases.setdefault(key, set())
    stem = base
    if stem in used:
        i = 2
        while f"{stem}_{i}" in used:
            i += 1
        stem = f"{stem}_{i}"
    used.add(stem)
    return stem + ".md"


async def _process_toc_item(
    client: YuqueClient,
    repo_id: int,
    repo_dir_name: str,
    output_dir: Path,
    toc_item: Dict,
    parent_path: str,
    toc_by_uuid: Dict[str, Dict],
    index: Optional[Dict[str, str]] = None,
    used_bases: Optional[Dict[tuple, set]] = None,
) -> None:
    if used_bases is None:
        used_bases = {}
    doc_type = toc_item.get("type", "")
    slug = toc_item.get("url") or toc_item.get("slug") or toc_item.get("uuid", "")
    yuque_id = toc_item.get("id")
    if isinstance(yuque_id, str) and yuque_id.isdigit():
        yuque_id = int(yuque_id)

    if doc_type in ("DOC", "SHEET") and slug:
        detail = await client.get_doc_detail(repo_id, slug)
        if not detail:
            logger.warning("  skip doc (no detail): %s", toc_item.get("title"))
            return
        base = _doc_basename(detail.get("title"), slug or "")
        doc_filename = _resolve_doc_basename(used_bases, repo_dir_name, parent_path, base)
        if parent_path:
            out_file = output_dir / repo_dir_name / parent_path / doc_filename
        else:
            out_file = output_dir / repo_dir_name / doc_filename
        out_file.parent.mkdir(parents=True, exist_ok=True)
        rel_path = out_file.relative_to(output_dir).as_posix()
        if yuque_id is not None and index is not None:
            old_path = index.get(str(yuque_id))
            if old_path and old_path != rel_path:
                old_full = output_dir / old_path
                if old_full.exists():
                    old_full.unlink()
                    logger.info("  removed old path (move): %s", old_path)
        members = _read_members(output_dir)
        user_id_str = str(detail.get("last_editor_id") or detail.get("user_id") or "")
        author_name = ""
        if user_id_str and user_id_str in members:
            author_name = (members[user_id_str].get("name") or members[user_id_str].get("login") or "").strip()
        if not author_name:
            author_name = _author_name_from_detail(detail)
        content = _build_md(detail, author_name)
        out_file.write_text(content, encoding="utf-8")
        if yuque_id is not None and index is not None:
            index[str(yuque_id)] = rel_path
        logger.info("  wrote %s", out_file.relative_to(output_dir))
    elif doc_type == "TITLE":
        seg = _slug_safe(toc_item.get("title") or toc_item.get("uuid", ""))
        next_parent = f"{parent_path}/{seg}" if parent_path else seg
        (output_dir / repo_dir_name / next_parent).mkdir(parents=True, exist_ok=True)
        for child in toc_list_children(toc_item.get("uuid"), toc_by_uuid):
            await _process_toc_item(client, repo_id, repo_dir_name, output_dir, child, next_parent, toc_by_uuid, index, used_bases)


def _next_sibling(node: Dict, toc_by_uuid: Dict[str, Dict]) -> Optional[Dict]:
    """沿 sibling_uuid 取下一个兄弟节点（兼容 child_uuid/sibling_uuid 字段名）。"""
    uuid = node.get("sibling_uuid") or node.get("siblingUuid")
    if not uuid:
        return None
    return toc_by_uuid.get(uuid)


def toc_list_children(parent_uuid: Optional[str], toc_by_uuid: Dict[str, Dict]) -> List[Dict]:
    """返回父节点下的子节点列表。若有 child_uuid/sibling_uuid 链表则按语雀顺序，否则按原逻辑（顺序未保证）。"""
    out: List[Dict] = []
    if parent_uuid is None:
        roots = [n for n in toc_by_uuid.values() if n.get("parent_uuid") in (None, "")]
        if not roots:
            return out
        sibling_targets = {n.get("sibling_uuid") or n.get("siblingUuid") for n in roots if n.get("sibling_uuid") or n.get("siblingUuid")}
        first_uuid = None
        for n in roots:
            u = n.get("uuid")
            if u and u not in sibling_targets:
                first_uuid = u
                break
        if first_uuid:
            node = toc_by_uuid.get(first_uuid)
            while node:
                out.append(node)
                node = _next_sibling(node, toc_by_uuid)
        if len(out) != len(roots):
            out = roots
    else:
        parent = toc_by_uuid.get(parent_uuid)
        start_uuid = None
        if parent:
            start_uuid = parent.get("child_uuid") or parent.get("childUuid")
        if start_uuid and start_uuid in toc_by_uuid:
            node = toc_by_uuid[start_uuid]
            while node:
                if (node.get("parent_uuid") or node.get("parentUuid")) in (parent_uuid, None):
                    out.append(node)
                node = _next_sibling(node, toc_by_uuid)
        if not out:
            for n in toc_by_uuid.values():
                if (n.get("parent_uuid") or n.get("parentUuid")) == parent_uuid:
                    out.append(n)
    return out


def _read_id_to_path_index(output_dir: Path) -> Dict[str, str]:
    p = output_dir / ".yuque-id-to-path.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_id_to_path_index(output_dir: Path, data: Dict[str, str]) -> None:
    p = output_dir / ".yuque-id-to-path.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_last_push(output_dir: Path) -> Dict[str, str]:
    """与 webhook 同路径、同格式：key=yuque_id(str), value=commit hash。"""
    p = output_dir / ".yuque-last-push.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_last_push(output_dir: Path, data: Dict[str, str]) -> None:
    p = output_dir / ".yuque-last-push.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def sync_repo(
    client: YuqueClient,
    repo: Dict,
    output_dir: Path,
    index: Optional[Dict[str, str]] = None,
) -> None:
    repo_id = repo["id"]
    # 与 webhook 一致：目录用知识库名称，不用 slug
    repo_dir_name = _slug_safe(repo.get("name", "") or repo.get("slug", "") or str(repo_id))
    repo_dir = output_dir / repo_dir_name
    repo_dir.mkdir(parents=True, exist_ok=True)

    toc_list = await client.get_repo_toc(repo_id)
    logger.info("Syncing repo: %s", repo_dir_name)
    toc_file = repo_dir / ".toc.json"
    toc_file.write_text(json.dumps(toc_list, ensure_ascii=False, indent=2), encoding="utf-8")

    toc_by_uuid = {n["uuid"]: n for n in toc_list if n.get("uuid")}
    roots = toc_list_children(None, toc_by_uuid)
    used_bases: Dict[tuple, set] = {}
    for item in roots:
        await _process_toc_item(client, repo_id, repo_dir_name, output_dir, item, "", toc_by_uuid, index, used_bases)

    # 清理：删除本仓库下不在当前 TOC 中的 .md（语雀已删或移走的孤儿文件）
    valid_paths = {p for p in (index or {}).values() if p.startswith(repo_dir_name + "/")}
    last_push = _read_last_push(output_dir)
    last_push_modified = False
    for md_file in repo_dir.rglob("*.md"):
        rel = str(md_file.relative_to(output_dir))
        if md_file.relative_to(output_dir).as_posix() not in valid_paths:
            md_file.unlink()
            if index is not None:
                for k in list(index.keys()):
                    if index.get(k) == md_file.relative_to(output_dir).as_posix():
                        del index[k]
                        if k in last_push:
                            del last_push[k]
                            last_push_modified = True
                        break
            logger.info("  removed orphan: %s", md_file.relative_to(output_dir).as_posix())
    if last_push_modified:
        _write_last_push(output_dir, last_push)

    # 清理仅含 .toc.json 的非法子目录（本脚本只在仓库根写 .toc.json，子目录下仅有 .toc.json 为残留）
    for _ in range(10):  # 多轮，因子目录删除后父目录可能也变成仅 .toc.json
        removed_any = False
        for d in sorted(repo_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if not d.is_dir() or d == repo_dir:
                continue
            items = list(d.iterdir())
            if len(items) == 1 and items[0].name == ".toc.json" and items[0].is_file():
                items[0].unlink()
                try:
                    d.rmdir()
                    logger.info("  removed dir with only .toc.json: %s", d.relative_to(output_dir))
                    removed_any = True
                except OSError:
                    pass
        if not removed_any:
            break


async def main_async(output_dir: Path, repo_id: Optional[int], mark_all_pushed: bool) -> None:
    if not YUQUE_TOKEN:
        raise SystemExit("YUQUE_TOKEN required")

    client = YuqueClient()
    user = await client.get_user()
    if not user:
        raise SystemExit("Failed to get user")
    user_id = user.get("id")
    if not user_id:
        raise SystemExit("No user id")

    repos = await client.get_user_repos(user_id)
    if repo_id is not None:
        repos = [r for r in repos if r.get("id") == repo_id]
    if not repos:
        logger.info("No repos to sync")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    if not (output_dir / ".git").exists():
        subprocess.run(["git", "init"], cwd=output_dir, check=True, capture_output=True)

    await fetch_and_write_team_members(client, user_id, output_dir)

    index = _read_id_to_path_index(output_dir)
    repos_meta = []
    for i, repo in enumerate(repos):
        if i > 0 and YUQUE_SYNC_REQUEST_DELAY > 0:
            await asyncio.sleep(YUQUE_SYNC_REQUEST_DELAY * 2)
        repo_dir_name = _slug_safe(repo.get("name", "") or repo.get("slug", "") or str(repo["id"]))
        await sync_repo(client, repo, output_dir, index)
        repos_meta.append({"id": repo["id"], "slug": repo.get("slug"), "name": repo.get("name"), "dir": repo_dir_name})
    _write_id_to_path_index(output_dir, index)

    (output_dir / ".repos.json").write_text(
        json.dumps(repos_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    current_dirs = {r["dir"] for r in repos_meta}
    for d in output_dir.iterdir():
        if d.name.startswith(".") or not d.is_dir():
            continue
        if d.name not in current_dirs:
            logger.info("Orphan repo dir (not in current API list): %s", d.name)
    subprocess.run(
        ["git", "add", "-A"],
        cwd=output_dir,
        check=True,
        capture_output=True,
    )
    commit_ret = subprocess.run(
        ["git", "commit", "-m", "yuque sync: full sync"],
        cwd=output_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if commit_ret.returncode != 0:
        msg = (commit_ret.stderr or commit_ret.stdout or "unknown").strip() or "nothing to commit"
        logger.warning("git commit failed (returncode %s): %s", commit_ret.returncode, msg)
    logger.info("Full sync done. Repos: %s", [r["dir"] for r in repos_meta])
    if mark_all_pushed:
        logger.info("--mark-all-pushed: TODO update .yuque-last-push.json with current commit")


def main():
    parser = argparse.ArgumentParser(description="yuque2git full sync")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=os.environ.get("OUTPUT_DIR"),
        help="文档存储目录（Git 仓库根），与 webhook 服务一致；也可设环境变量 OUTPUT_DIR",
    )
    parser.add_argument("--repo-id", type=int, default=None, help="Sync only this repo id")
    parser.add_argument("--mark-all-pushed", action="store_true", help="Set last-push for all docs to current commit")
    args = parser.parse_args()
    if not args.output_dir:
        parser.error("--output-dir 或环境变量 OUTPUT_DIR 必填")
    asyncio.run(main_async(Path(args.output_dir).resolve(), args.repo_id, args.mark_all_pushed))


if __name__ == "__main__":
    main()
