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
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

YUQUE_TOKEN = os.environ.get("YUQUE_TOKEN", "")
YUQUE_BASE_URL = os.environ.get("YUQUE_BASE_URL", "https://nova.yuque.com/api/v2").rstrip("/")
# 限流与重试
YUQUE_SYNC_CONCURRENCY = int(os.environ.get("YUQUE_SYNC_CONCURRENCY", "3"))
YUQUE_SYNC_REQUEST_DELAY = float(os.environ.get("YUQUE_SYNC_REQUEST_DELAY", "0.25"))
YUQUE_SYNC_MAX_RETRIES = int(os.environ.get("YUQUE_SYNC_MAX_RETRIES", "4"))
SEMAPHORE = asyncio.Semaphore(YUQUE_SYNC_CONCURRENCY)


def _slug_safe(s: str) -> str:
    for c in r'/\:*?"<>|':
        s = s.replace(c, "_")
    return s.strip() or "untitled"


def _parse_time(ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    return ts.replace("Z", "+00:00") if isinstance(ts, str) else None


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


def _build_md(detail: Dict[str, Any], author_name: str = "") -> str:
    fm = {k: v for k, v in detail.items() if k not in ("body", "body_html") and v is not None}
    created = detail.get("created_at") or ""
    updated = detail.get("updated_at") or detail.get("content_updated_at") or ""
    if isinstance(created, str) and "T" in created:
        created = created.replace("Z", "+00:00")[:19].replace("T", " ")
    if isinstance(updated, str) and "T" in updated:
        updated = updated.replace("Z", "+00:00")[:19].replace("T", " ")

    yaml_block = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
    def esc(x):
        return (x or "").replace("|", "\\|").replace("\n", " ").strip()
    md = "---\n" + yaml_block + "\n---\n\n"
    md += "| 作者 | 创建时间 | 更新时间 |\n|------|----------|----------|\n"
    md += f"| {esc(author_name)} | {esc(str(created))} | {esc(str(updated))} |\n\n"
    md += (detail.get("body") or "").strip()
    if not md.rstrip():
        pass
    elif not md.endswith("\n"):
        md += "\n"
    return md


async def _process_toc_item(
    client: YuqueClient,
    repo_id: int,
    repo_slug: str,
    output_dir: Path,
    toc_item: Dict,
    parent_path: str,
    toc_by_uuid: Dict[str, Dict],
    index: Optional[Dict[str, str]] = None,
) -> None:
    doc_type = toc_item.get("type", "")
    slug = toc_item.get("url") or toc_item.get("slug") or toc_item.get("uuid", "")
    yuque_id = toc_item.get("id")
    if isinstance(yuque_id, str) and yuque_id.isdigit():
        yuque_id = int(yuque_id)

    if doc_type == "DOC" and slug:
        detail = await client.get_doc_detail(repo_id, slug)
        if not detail:
            logger.warning("  skip doc (no detail): %s", toc_item.get("title"))
            return
        if parent_path:
            out_file = output_dir / repo_slug / parent_path / (_slug_safe(slug) + ".md")
        else:
            out_file = output_dir / repo_slug / (_slug_safe(slug) + ".md")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        rel_path = str(out_file.relative_to(output_dir))
        if yuque_id is not None and index is not None:
            old_path = index.get(str(yuque_id))
            if old_path and old_path != rel_path:
                old_full = output_dir / old_path
                if old_full.exists():
                    old_full.unlink()
                    logger.info("  removed old path (move): %s", old_path)
        content = _build_md(detail, "")
        out_file.write_text(content, encoding="utf-8")
        if yuque_id is not None and index is not None:
            index[str(yuque_id)] = rel_path
        logger.info("  wrote %s", out_file.relative_to(output_dir))
    elif doc_type == "TITLE" or doc_type == "SHEET":
        seg = _slug_safe(toc_item.get("title") or toc_item.get("uuid", ""))
        next_parent = f"{parent_path}/{seg}" if parent_path else seg
        (output_dir / repo_slug / next_parent).mkdir(parents=True, exist_ok=True)
        for child in toc_list_children(toc_item.get("uuid"), toc_by_uuid):
            await _process_toc_item(client, repo_id, repo_slug, output_dir, child, next_parent, toc_by_uuid, index)


def toc_list_children(parent_uuid: Optional[str], toc_by_uuid: Dict[str, Dict]) -> List[Dict]:
    out = []
    for n in toc_by_uuid.values():
        pu = n.get("parent_uuid")
        if pu == parent_uuid:
            out.append(n)
        elif parent_uuid is None and (pu is None or pu == ""):
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
    toc_file = repo_dir / ".toc.json"
    toc_file.write_text(json.dumps(toc_list, ensure_ascii=False, indent=2), encoding="utf-8")

    toc_by_uuid = {n["uuid"]: n for n in toc_list if n.get("uuid")}
    roots = toc_list_children(None, toc_by_uuid)
    for item in roots:
        await _process_toc_item(client, repo_id, repo_dir_name, output_dir, item, "", toc_by_uuid, index)


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
    subprocess.run(
        ["git", "add", "-A"],
        cwd=output_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "yuque sync: full sync"],
        cwd=output_dir,
        capture_output=True,
    )
    logger.info("Full sync done. Repos: %s", [r["slug"] for r in repos_meta])
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
