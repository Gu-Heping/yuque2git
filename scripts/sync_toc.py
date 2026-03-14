#!/usr/bin/env python3
"""
yuque2git TOC 同步：遍历已有 repo（从 .repos.json 或 output_dir 子目录），拉取 GET /repos/{id}/toc 写回 .toc.json 并 commit。
"""
import argparse
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

YUQUE_TOKEN = os.environ.get("YUQUE_TOKEN", "")
YUQUE_BASE_URL = os.environ.get("YUQUE_BASE_URL", "https://nova.yuque.com/api/v2").rstrip("/")


def _slug_safe(s: str) -> str:
    for c in r'/\:*?"<>|':
        s = s.replace(c, "_")
    return (s or "").strip() or "untitled"


def get_repos(output_dir: Path) -> List[dict]:
    """从 .repos.json 或子目录名推断（无 id 时仅能靠目录名，需 .repos.json 有 id）。"""
    repos_file = output_dir / ".repos.json"
    if repos_file.exists():
        try:
            data = json.loads(repos_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            pass
    out = []
    for d in output_dir.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            out.append({"slug": d.name, "id": None})
    return out


async def fetch_toc(repo_id: int) -> Optional[list]:
    async with httpx.AsyncClient(
        headers={
            "X-Auth-Token": YUQUE_TOKEN,
            "User-Agent": "yuque2git/1.0",
        },
        timeout=30.0,
    ) as client:
        r = await client.get(f"{YUQUE_BASE_URL}/repos/{repo_id}/toc")
        if r.status_code != 200:
            return None
        return r.json().get("data", [])


def main():
    import asyncio

    parser = argparse.ArgumentParser(description="yuque2git TOC sync")
    parser.add_argument("--output-dir", type=Path, required=True, help="Output Git repo root")
    parser.add_argument("--repo-id", type=int, default=None, help="Sync only this repo id")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    if not output_dir.is_dir():
        raise SystemExit("output-dir must exist")
    if not YUQUE_TOKEN:
        raise SystemExit("YUQUE_TOKEN required")

    repos = get_repos(output_dir)
    if args.repo_id is not None:
        repos = [r for r in repos if r.get("id") == args.repo_id]
    if not repos:
        logger.info("No repos found (create .repos.json or run full sync first)")
        return

    async def run():
        for repo in repos:
            rid = repo.get("id")
            slug = repo.get("slug", "")
            dir_name = repo.get("dir") or _slug_safe(slug or str(rid or ""))
            if rid is None:
                logger.warning("Skip %s: no repo id in .repos.json", dir_name)
                continue
            toc = await fetch_toc(rid)
            if toc is None:
                logger.warning("Failed to fetch TOC for repo %s (%s)", dir_name, rid)
                continue
            toc_file = output_dir / dir_name / ".toc.json"
            toc_file.parent.mkdir(parents=True, exist_ok=True)
            toc_file.write_text(json.dumps(toc, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Wrote %s", toc_file.relative_to(output_dir))

        subprocess.run(["git", "add", "-A"], cwd=output_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "yuque toc: sync"],
            cwd=output_dir,
            capture_output=True,
        )

    asyncio.run(run())
    logger.info("TOC sync done")


if __name__ == "__main__":
    main()
