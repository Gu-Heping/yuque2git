#!/usr/bin/env python3
"""
yuque2git Webhook 服务：接收语雀 Webhook，写本地 Markdown + Git commit，支持智能推送（LLM / OpenClaw）。
"""
import argparse
import asyncio
import difflib
import fcntl
import json
import logging
import os
import re
import smtplib
import time
import subprocess
import zlib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from string import Formatter
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OpenClaw 默认 message 首段原则；自定义模板可用占位符 {push_policy}
OPENCLAW_PUSH_POLICY_SHORT = (
    "【重要】以降低通知频率、提高推送质量为目标：仅在阶段性/实质性内容更新，或 diff 体现重要信息时建议推送；"
    "琐碎修改请回调 should_push=false。"
)
# 写入 reply_contract 的详细门槛（与 Agent 契约一体）
OPENCLAW_PUSH_GATE_RULES = (
    "【推送门槛（请严格遵守）】\n"
    "- 默认保守：无把握时一律 should_push=false，避免打扰订阅者。\n"
    "- 应 should_push=false：错别字或标点微调、纯排版或仅空格换行、单句措辞润色、无关紧要的链接或元数据微调、"
    "极小片段修改且无信息增量。\n"
    "- 可 should_push=true：新增或重写整节、多段逻辑或结构变化、重要结论/决策/数据/API 或接口约定变更、"
    "明显「阶段性成果」的篇幅与语义变化；若 diff 极少但信息极重要也可为 true，且 highlights 须写明原因。\n\n"
)

# 文件日志路径（在 main() 中挂载）
_WEBHOOK_LOG_FILE: Optional[Path] = None

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
# 多目标：JSON 数组 [{"channel":"qq","to":"g:群号"}, {"channel":"discord","to":"channel:频道ID"}, ...]；
# 未设时由 CHANNEL+TO 解析（TO 可逗号分隔多个同 channel 目标）
YUQUE2GIT_DELIVER_TARGETS_JSON = os.environ.get("YUQUE2GIT_DELIVER_TARGETS", "").strip()
# 多目标时两次 POST 之间的间隔（秒），避免连续请求触发 Gateway/上游 rate limit，默认 2
YUQUE2GIT_DELIVER_DELAY_SECONDS = max(0.0, float(os.environ.get("YUQUE2GIT_DELIVER_DELAY_SECONDS", "2.0")))
# OpenClaw/Gateway 返回 429 时的最大重试次数（每次指数退避），默认 3
YUQUE2GIT_DELIVER_MAX_RETRIES = max(0, int(os.environ.get("YUQUE2GIT_DELIVER_MAX_RETRIES", "3")))
# 直连投递（不经 OpenClaw Agent 轮次发最终摘要文案）：
# - Napcat OneBot：配置 YUQUE2GIT_DIRECT_SEND_URL（+ 可选 TOKEN），仅处理 deliver channel=qq
# - Discord Bot REST：配置 YUQUE2GIT_DISCORD_BOT_TOKEN（或回退 DISCORD_BOT_TOKEN），仅处理 channel=discord
# 二者可同时配置：按目标 channel 分别走对应后端；无法直连的目标回退到 OpenClaw /hooks/agent（若已配置）
YUQUE2GIT_DIRECT_SEND_URL = os.environ.get("YUQUE2GIT_DIRECT_SEND_URL", "").strip().rstrip("/")
YUQUE2GIT_DIRECT_SEND_TOKEN = os.environ.get("YUQUE2GIT_DIRECT_SEND_TOKEN", "").strip()
YUQUE2GIT_DISCORD_BOT_TOKEN = (
    os.environ.get("YUQUE2GIT_DISCORD_BOT_TOKEN", "").strip()
    or os.environ.get("DISCORD_BOT_TOKEN", "").strip()
)
YUQUE2GIT_DISCORD_API_BASE = (
    os.environ.get("YUQUE2GIT_DISCORD_API_BASE", "https://discord.com/api/v10").strip().rstrip("/")
    or "https://discord.com/api/v10"
)
# 仅 Discord Bot REST 使用（httpx proxy）；勿用全局 HTTP(S)_PROXY 代替，以免语雀 API、OpenClaw 本地回调等出站误走代理
YUQUE2GIT_DISCORD_HTTP_PROXY = os.environ.get("YUQUE2GIT_DISCORD_HTTP_PROXY", "").strip()
YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE = os.environ.get("YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE", "").strip()
# OpenClaw 降噪：diff 字符数低于此值则不调用 OpenClaw（0=关闭）
YUQUE2GIT_OPENCLAW_MIN_DIFF_CHARS = max(0, int(os.environ.get("YUQUE2GIT_OPENCLAW_MIN_DIFF_CHARS", "0")))
# 成功投递摘要后，同一 yuque_id 在此秒数内不再调用 OpenClaw（0=关闭）；大 diff 可绕过见下一项
YUQUE2GIT_OPENCLAW_COOLDOWN_SECONDS = max(0, int(os.environ.get("YUQUE2GIT_OPENCLAW_COOLDOWN_SECONDS", "0")))
# 冷却期内若 diff 字符数 ≥ 此值仍调用 OpenClaw（0=不启用绕过）
YUQUE2GIT_OPENCLAW_COOLDOWN_BYPASS_CHARS = max(0, int(os.environ.get("YUQUE2GIT_OPENCLAW_COOLDOWN_BYPASS_CHARS", "0")))
# 语雀文档原文地址：用于生成 doc_url，未设时从 detail.book.user.login 取
YUQUE_NAMESPACE = os.environ.get("YUQUE_NAMESPACE", "").strip()
# 元数据时间转为本地可读：YUQUE_TIMEZONE（如 Asia/Shanghai），未设默认 Asia/Shanghai
YUQUE_TIMEZONE = os.environ.get("YUQUE_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
NOTIFY_URL = os.environ.get("NOTIFY_URL", "").strip()
LAST_PUSH_FILE = ".yuque-last-push.json"  # key: yuque_id (str), value: commit hash
OPENCLAW_COOLDOWN_FILE = ".yuque-openclaw-push-cooldown.json"  # key: yuque_id (str), value: unix ts 上次成功投递摘要
IDX_FILE = ".yuque-id-to-path.json"  # key: yuque_id (str), value: rel_path，用于文档移动时 diff 查旧路径
MEMBERS_FILE = ".yuque-members.json"  # key: user_id (str), value: {"name": "...", "login": "..."}，本地缓存避免重复请求
PENDING_PUSH_FILE = os.environ.get("PENDING_PUSH_FILE", ".yuque-pending-pushes.jsonl").strip() or ".yuque-pending-pushes.jsonl"
PENDING_REPLAY_LOCK_FILE = ".yuque-replay.lock"
REPLAY_MARK_PUSHED_BASE_URL = (os.environ.get("YUQUE2GIT_PUBLIC_URL", "").strip() or "http://127.0.0.1:8765").rstrip("/")
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
    yuque_id: Union[int, str]
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


def _creator_user_id(detail: Dict[str, Any]) -> Optional[int]:
    """文档创建者用户 id：creator.id → user_id → user.id（不使用 last_editor）。"""
    c = detail.get("creator")
    if isinstance(c, dict) and c.get("id") is not None:
        try:
            return int(c["id"])
        except (TypeError, ValueError):
            pass
    uid = detail.get("user_id")
    if uid is not None:
        try:
            return int(uid)
        except (TypeError, ValueError):
            pass
    u = detail.get("user")
    if isinstance(u, dict) and u.get("id") is not None:
        try:
            return int(u["id"])
        except (TypeError, ValueError):
            pass
    return None


def _creator_name_from_detail(detail: Dict[str, Any]) -> str:
    """从文档详情中取创建者显示名：仅 creator / user 嵌套对象（不用 last_editor）。"""
    for key in ("creator", "user"):
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
) -> str:
    """解析文档创建者显示名：.yuque-members.json → 详情嵌套 creator/user → GET /users/:id（创建者 id）。"""
    creator_id = _creator_user_id(detail)
    creator_id_str = str(creator_id) if creator_id is not None else ""
    members = _read_members(output_dir)
    if creator_id_str and creator_id_str in members:
        return (members[creator_id_str].get("name") or members[creator_id_str].get("login") or "").strip()
    name = _creator_name_from_detail(detail)
    if name:
        return name
    if creator_id is None:
        return ""
    try:
        user_data = await client.get_user_by_id(creator_id)
        if user_data:
            entry = {
                "name": (user_data.get("name") or "").strip(),
                "login": (user_data.get("login") or "").strip(),
            }
            members[creator_id_str] = entry
            _write_members(output_dir, members)
            return (entry["name"] or entry["login"] or "").strip()
    except Exception as e:
        logger.warning("get_user_by_id %s failed: %s", creator_id, e)
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


def _build_md(detail: Dict[str, Any], author_name: str = "", members: Optional[Dict] = None) -> str:
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
    md += _render_doc_body(detail, members)
    if not md.endswith("\n"):
        md += "\n"
    return md


def _render_doc_body(detail: Dict[str, Any], members: Optional[Dict] = None) -> str:
    fmt = (detail.get("format") or "").lower()
    typ = (detail.get("type") or "").lower()
    if fmt == "lakesheet" or typ == "sheet":
        rendered = _render_lakesheet_markdown(detail.get("body") or "")
        if rendered:
            return rendered
    if fmt == "laketable" or typ == "table":
        rendered = _render_laketable_markdown(detail, members)
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


def _render_laketable_markdown(detail: Dict[str, Any], members: Optional[Dict] = None) -> str:
    """将 laketable 多维表格转换为可读 TSV 格式

    语雀 laketable 数据分布在两个字段：
    - body: 表格元数据（列定义、视图、行 ID 列表）
    - body_table: 实际数据（records 数组，每条记录的 values 按列顺序存储）
    """
    body = detail.get("body") or ""
    body_table = detail.get("body_table") or ""

    try:
        body_data = json.loads(body) if isinstance(body, str) else body
        table_data = json.loads(body_table) if isinstance(body_table, str) else body_table
    except (TypeError, json.JSONDecodeError):
        return str(body or "").strip()

    sheet = body_data.get("sheet", [{}])[0] if body_data.get("sheet") else {}
    columns = sheet.get("columns", [])
    records = table_data.get("records", []) if isinstance(table_data, dict) else []

    if not columns:
        # 没有列定义，返回原始 JSON
        return json.dumps(body_data, ensure_ascii=False, indent=2)

    blocks: List[str] = [
        "> Auto-generated from Yuque laketable for readable review and diff.",
        "",
    ]

    sheet_name = sheet.get("name") or detail.get("title") or "Table"
    blocks.append(f"## {sheet_name}")
    blocks.append("")
    blocks.append("```tsv")

    # 表头
    col_names = [c.get("name", c.get("id", "")) for c in columns]
    blocks.append("\t".join(col_names))

    # 数据行
    if not records:
        blocks.append("(empty)")
    else:
        for rec in records:
            values = rec.get("values", [])
            cells = []
            for i, col in enumerate(columns):
                cell_value = _laketable_value_to_text(values[i] if i < len(values) else None, col, members)
                cells.append(cell_value)
            blocks.append("\t".join(cells))

    blocks.append("```")
    blocks.append("")

    return "\n".join(blocks).strip()


def _laketable_value_to_text(cell: Any, col_def: Dict, members: Optional[Dict] = None) -> str:
    """将 laketable 单元格值转换为文本"""
    if cell is None:
        return ""

    # 处理 {"value": ...} 包装
    if isinstance(cell, dict) and "value" in cell:
        value = cell["value"]
    else:
        value = cell

    if value is None:
        return ""

    col_type = col_def.get("type", "")

    if col_type == "mention":
        # 提及用户：优先从团队成员缓存获取姓名，多个用逗号分隔
        if isinstance(value, list):
            names = []
            for u in value:
                if isinstance(u, dict):
                    uid = u.get("id")
                    uid_str = str(uid) if uid else ""
                    # 优先使用团队内姓名
                    if members and uid_str in members:
                        names.append(members[uid_str].get("name") or members[uid_str].get("login") or u.get("name") or u.get("login", ""))
                    else:
                        names.append(u.get("name", u.get("login", "")))
            return ", ".join(names)
        return str(value)

    if col_type == "select":
        # 单选：查找选项值
        options = col_def.get("options", [])
        for opt in options:
            if opt.get("id") == value:
                return opt.get("value", value)
        return str(value) if value else ""

    if col_type == "date":
        # 日期：使用 text 字段
        if isinstance(value, dict):
            return value.get("text", value.get("time", ""))
        return str(value)

    if col_type == "link":
        # 链接：显示 URL 或文本
        if isinstance(value, dict):
            return value.get("link", value.get("text", ""))
        return str(value)

    if col_type == "multiSelect":
        # 多选：逗号分隔
        if isinstance(value, list):
            opt_ids = value
            options = col_def.get("options", [])
            names = []
            for opt in options:
                if opt.get("id") in opt_ids:
                    names.append(opt.get("value", opt.get("id")))
            return ", ".join(names)
        return str(value)

    # 默认：直接转字符串
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return value.get("text", value.get("name", json.dumps(value, ensure_ascii=False)))
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


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


def _doc_meta_from_md(file_path: Path) -> Dict[str, Any]:
    """读取文档 frontmatter 元数据，失败时返回空 dict。"""
    try:
        raw = file_path.read_text(encoding="utf-8")
        if not raw.startswith("---"):
            return {}
        end = raw.index("---", 3) if "---" in raw[3:] else -1
        if end <= 0:
            return {}
        fm = yaml.safe_load(raw[3:end])
        return fm if isinstance(fm, dict) else {}
    except Exception:
        return {}


def _resolve_yuque_id_for_mark_pushed(
    output_dir: Path,
    raw_yuque_id: Union[int, str],
    repo_slug: Optional[str],
    doc_slug: Optional[str],
) -> Tuple[Optional[int], str]:
    """兼容 mark-pushed 的 yuque_id 误参（如传 slug）并返回解析来源。"""
    if isinstance(raw_yuque_id, int):
        return raw_yuque_id, "int"
    if isinstance(raw_yuque_id, str) and raw_yuque_id.isdigit():
        return int(raw_yuque_id), "numeric-string"

    candidate_slug = (doc_slug or "").strip() or (str(raw_yuque_id).strip() if isinstance(raw_yuque_id, str) else "")
    if not candidate_slug:
        return None, "invalid-empty"

    # 先尝试按索引映射到路径，再读取 frontmatter 获取真实 id。
    idx = _read_index(output_dir)
    search_paths: List[str] = []
    if repo_slug:
        repo_prefix = _slug_safe(repo_slug).rstrip("/") + "/"
        search_paths.extend([p for p in idx.values() if isinstance(p, str) and p.startswith(repo_prefix)])
    search_paths.extend([p for p in idx.values() if isinstance(p, str) and p not in search_paths])

    for rel in search_paths:
        md = output_dir / rel
        fm = _doc_meta_from_md(md)
        if not fm:
            continue
        fm_slug = str(fm.get("slug") or "").strip()
        if fm_slug != candidate_slug:
            continue
        doc_id = fm.get("id") if fm.get("id") is not None else fm.get("yuque_id")
        if isinstance(doc_id, int):
            return doc_id, "slug-frontmatter-index"
        if isinstance(doc_id, str) and doc_id.isdigit():
            return int(doc_id), "slug-frontmatter-index"

    # 索引里未命中时，回退到遍历仓库目录。
    repo_dir = output_dir / _slug_safe(repo_slug) if repo_slug else output_dir
    scan_dirs = [repo_dir] if repo_slug and repo_dir.is_dir() else [d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    for base in scan_dirs:
        for md in base.rglob("*.md"):
            fm = _doc_meta_from_md(md)
            if not fm:
                continue
            fm_slug = str(fm.get("slug") or "").strip()
            if fm_slug != candidate_slug:
                continue
            doc_id = fm.get("id") if fm.get("id") is not None else fm.get("yuque_id")
            if isinstance(doc_id, int):
                return doc_id, "slug-frontmatter-scan"
            if isinstance(doc_id, str) and doc_id.isdigit():
                return int(doc_id), "slug-frontmatter-scan"
    return None, "unresolved"


def _append_pending_push_event(output_dir: Path, event: Dict[str, Any]) -> None:
    """将失败事件落盘为 JSONL，便于后续重放。"""
    p = output_dir / PENDING_PUSH_FILE
    payload = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("write pending push queue failed: %s", e)


def _replay_dedup_key(evt: Dict[str, Any]) -> str:
    """重放去重键：type + yuque_id + commit + target。"""
    t = evt.get("type") or ""
    yid = evt.get("yuque_id") or evt.get("raw_yuque_id") or ""
    commit = evt.get("commit") or ""
    target = ""
    if "target_channel" in evt or "target_to" in evt:
        target = f"{evt.get('target_channel', '')}:{evt.get('target_to', '')}"
    elif evt.get("request_body"):
        rb = evt["request_body"]
        target = f"{rb.get('channel', '')}:{rb.get('to', '')}"
    return f"{t}|{yid}|{commit}|{target}"


def _append_replay_status(output_dir: Path, result: Dict[str, Any]) -> None:
    """追加重放结果行（仅追加，不修改原行）。"""
    p = output_dir / PENDING_PUSH_FILE
    payload = {"ts": datetime.now(timezone.utc).isoformat(), **result}
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("append replay status failed: %s", e)


async def _replay_pending_async(output_dir: Path, limit: int) -> Tuple[int, int, int]:
    """
    读取 pending JSONL，按类型重放 mark_pushed_invalid_* 与 openclaw_*_failed，追加 status 行。
    返回 (done_count, retry_failed_count, invalid_payload_count)。
    使用文件锁防多实例；按 type+yuque_id+commit+target 去重；4xx 标 invalid_payload，网络/5xx/429 指数退避。
    """
    p = output_dir / PENDING_PUSH_FILE
    if not p.exists():
        logger.info("replay: no pending file %s", p)
        return 0, 0, 0

    lock_path = output_dir / PENDING_REPLAY_LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_f = open(lock_path, "a")
    except OSError as e:
        logger.error("replay: cannot open lock file %s: %s", lock_path, e)
        return 0, 0, 0

    try:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
    except OSError as e:
        logger.error("replay: cannot acquire lock: %s", e)
        lock_f.close()
        return 0, 0, 0

    done_keys: set = set()
    replay_candidates: List[Dict[str, Any]] = []
    seen_keys: set = set()

    try:
        lines = p.read_text(encoding="utf-8").strip().split("\n")
    except Exception as e:
        logger.error("replay: read pending file failed: %s", e)
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        lock_f.close()
        return 0, 0, 0

    for raw in lines:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if row.get("status") == "done" and row.get("dedup_key"):
            done_keys.add(row["dedup_key"])
        if row.get("status") == "invalid_payload" and row.get("dedup_key"):
            done_keys.add(row["dedup_key"])

    for raw in lines:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if row.get("status") in ("done", "invalid_payload"):
            continue
        t = row.get("type") or ""
        if not (t.startswith("mark_pushed_invalid_") or t in (
            "openclaw_callback_delivery_failed",
            "openclaw_callback_exception",
            "openclaw_summary_delivery_failed",
            "openclaw_summary_delivery_exception",
        )):
            continue
        if t == "openclaw_callback_exception":
            continue
        key = _replay_dedup_key(row)
        if key in done_keys or key in seen_keys:
            continue
        seen_keys.add(key)
        replay_candidates.append(row)
        if len(replay_candidates) >= limit:
            break

    done_count = 0
    retry_failed_count = 0
    invalid_count = 0
    max_backoff_attempts = 3
    headers = {}
    if OPENCLAW_HOOKS_TOKEN:
        headers["Authorization"] = f"Bearer {OPENCLAW_HOOKS_TOKEN}"

    for evt in replay_candidates:
        key = _replay_dedup_key(evt)
        t = evt.get("type") or ""
        attempt = 0
        last_status = None
        last_reason = ""

        if t.startswith("mark_pushed_invalid_"):
            if t == "mark_pushed_invalid_yuque_id":
                body = {
                    "yuque_id": evt.get("raw_yuque_id"),
                    "commit": evt.get("commit", ""),
                    "should_push": False,
                    "repo_slug": evt.get("repo_slug"),
                    "doc_slug": evt.get("doc_slug"),
                }
            else:
                body = {
                    "yuque_id": evt.get("yuque_id"),
                    "commit": evt.get("commit", ""),
                    "should_push": True,
                    "summary": evt.get("raw_summary"),
                }
            while attempt <= max_backoff_attempts:
                attempt += 1
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        r = await client.post(
                            f"{REPLAY_MARK_PUSHED_BASE_URL}/mark-pushed",
                            json=body,
                        )
                    last_status = r.status_code
                    if 200 <= r.status_code < 300:
                        _append_replay_status(output_dir, {"status": "done", "dedup_key": key, "event_type": t})
                        done_count += 1
                        logger.info(
                            "replay event_type=%s reason=ok attempt=%s next_action=done",
                            t, attempt, extra={"event_type": t, "reason": "ok", "attempt": attempt, "next_action": "done"},
                        )
                        break
                    if 400 <= r.status_code < 500:
                        last_reason = f"http_{r.status_code}"
                        _append_replay_status(
                            output_dir,
                            {"status": "invalid_payload", "dedup_key": key, "event_type": t, "reason": last_reason},
                        )
                        invalid_count += 1
                        logger.warning(
                            "replay event_type=%s reason=%s attempt=%s next_action=invalid_payload",
                            t, last_reason, attempt,
                            extra={"event_type": t, "reason": last_reason, "attempt": attempt, "next_action": "invalid_payload"},
                        )
                        break
                    last_reason = f"http_{r.status_code}"
                except (httpx.TimeoutException, httpx.RequestError) as e:
                    last_reason = f"request_error:{type(e).__name__}"
                if attempt <= max_backoff_attempts:
                    wait = 2 ** (attempt - 1)
                    logger.warning(
                        "replay event_type=%s reason=%s attempt=%s next_action=retry_after_%ss",
                        t, last_reason, attempt, wait,
                        extra={"event_type": t, "reason": last_reason, "attempt": attempt, "next_action": f"retry_after_{wait}s"},
                    )
                    await asyncio.sleep(wait)
            else:
                _append_replay_status(
                    output_dir,
                    {"status": "retry_failed", "dedup_key": key, "event_type": t, "reason": last_reason},
                )
                retry_failed_count += 1
                logger.warning(
                    "replay event_type=%s reason=%s attempt=%s next_action=retry_failed",
                    t, last_reason, attempt,
                    extra={"event_type": t, "reason": last_reason, "attempt": attempt, "next_action": "retry_failed"},
                )
            continue

        if t in ("openclaw_callback_delivery_failed", "openclaw_summary_delivery_failed", "openclaw_summary_delivery_exception"):
            if t in ("openclaw_summary_delivery_failed", "openclaw_summary_delivery_exception") and _direct_send_any_backend_configured():
                summary = evt.get("summary")
                if summary and isinstance(summary, dict):
                    if await _deliver_openclaw_summary(summary):
                        _append_replay_status(output_dir, {"status": "done", "dedup_key": key, "event_type": t})
                        done_count += 1
                        logger.info("replay event_type=%s direct_send=ok", t, extra={"event_type": t})
                        continue
                _append_replay_status(output_dir, {"status": "retry_failed", "dedup_key": key, "event_type": t, "reason": "direct_send_failed"})
                retry_failed_count += 1
                continue
            if not OPENCLAW_CALLBACK_URL:
                _append_replay_status(
                    output_dir,
                    {"status": "retry_failed", "dedup_key": key, "event_type": t, "reason": "OPENCLAW_CALLBACK_URL_unset"},
                )
                retry_failed_count += 1
                continue
            req_body = evt.get("request_body")
            if not req_body and t == "openclaw_summary_delivery_exception":
                summary = evt.get("summary")
                if summary and isinstance(summary, dict):
                    message = _format_openclaw_summary(summary)
                    targets = _parse_deliver_targets()
                    if targets:
                        req_body = {"message": message, "name": "yuque2git", "deliver": True, "channel": targets[0]["channel"], "to": targets[0]["to"]}
            if not req_body:
                _append_replay_status(
                    output_dir,
                    {"status": "retry_failed", "dedup_key": key, "event_type": t, "reason": "no_request_body"},
                )
                retry_failed_count += 1
                continue
            if t == "openclaw_callback_delivery_failed" and isinstance(req_body, dict):
                bodies = [req_body]
            elif isinstance(req_body, dict) and "channel" in req_body and "to" in req_body:
                bodies = [req_body]
            else:
                bodies = [req_body] if isinstance(req_body, dict) else []

            openclaw_done = False
            last_reason = ""
            attempt = 0
            for b in bodies:
                attempt = 0
                while attempt <= max_backoff_attempts:
                    attempt += 1
                    try:
                        async with httpx.AsyncClient(timeout=15.0) as client:
                            r = await client.post(OPENCLAW_CALLBACK_URL, json=b, headers=headers or None)
                        last_status = r.status_code
                        if 200 <= r.status_code < 300:
                            _append_replay_status(output_dir, {"status": "done", "dedup_key": key, "event_type": t})
                            done_count += 1
                            openclaw_done = True
                            logger.info(
                                "replay event_type=%s reason=ok attempt=%s next_action=done",
                                t, attempt, extra={"event_type": t, "reason": "ok", "attempt": attempt, "next_action": "done"},
                            )
                            break
                        if 400 <= r.status_code < 500:
                            last_reason = f"http_{r.status_code}"
                            _append_replay_status(
                                output_dir,
                                {"status": "invalid_payload", "dedup_key": key, "event_type": t, "reason": last_reason},
                            )
                            invalid_count += 1
                            logger.warning(
                                "replay event_type=%s reason=%s attempt=%s next_action=invalid_payload",
                                t, last_reason, attempt,
                                extra={"event_type": t, "reason": last_reason, "attempt": attempt, "next_action": "invalid_payload"},
                            )
                            openclaw_done = True
                            break
                        last_reason = f"http_{r.status_code}"
                    except (httpx.TimeoutException, httpx.RequestError) as e:
                        last_reason = f"request_error:{type(e).__name__}"
                    if attempt <= max_backoff_attempts:
                        wait = 2 ** (attempt - 1)
                        await asyncio.sleep(wait)
                if openclaw_done:
                    break
            if not openclaw_done:
                _append_replay_status(
                    output_dir,
                    {"status": "retry_failed", "dedup_key": key, "event_type": t, "reason": last_reason},
                )
                retry_failed_count += 1
                logger.warning(
                    "replay event_type=%s reason=%s next_action=retry_failed",
                    t, last_reason,
                    extra={"event_type": t, "reason": last_reason, "attempt": attempt, "next_action": "retry_failed"},
                )
            continue

    try:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    lock_f.close()
    logger.info(
        "replay finished done=%s retry_failed=%s invalid_payload=%s",
        done_count, retry_failed_count, invalid_count,
    )
    return done_count, retry_failed_count, invalid_count


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
    return OPENCLAW_PUSH_GATE_RULES + (
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
        "- highlights 必须是 1～3 条字符串，每条都要写清楚具体改了什么；建议 2～3 条要点，避免仅一条笼统概括导致推送内容过简。\n"
        "- 禁止返回额外说明、Markdown 代码块或自然语言正文。\n"
        "- 再次强调：琐碎修改必须 should_push=false；仅当变更值得让订阅者专门打开文档时再 should_push=true。"
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
    """将结构化摘要渲染为最终投递文案，统一包含：标题、作者、知识库、编号要点、原文链接。"""
    author = (summary.get("author") or "").strip()
    title = (summary.get("title") or "").strip()
    repo_name = (summary.get("repo_name") or "").strip()
    doc_url = (summary.get("doc_url") or "").strip()
    highlights = list(summary.get("highlights") or [])
    lines = ["语雀文档更新通知：", ""]
    if author and title:
        repo_part = f"（知识库：{repo_name}）" if repo_name else ""
        lines.append(f"{author} 更新了《{title}》{repo_part}，主要变更如下：")
    else:
        lines.append(f"《{title or '文档'}》更新，主要变更如下：")
    lines.append("")
    for idx, item in enumerate(highlights, start=1):
        lines.append(f"{idx}. {item}")
    if doc_url:
        lines.append("")
        lines.append(f"原文：{doc_url}")
    return "\n".join(lines)


def _update_last_push_for(output_dir: Path, yuque_id: int, commit: str) -> None:
    data = _read_last_push(output_dir)
    data[str(yuque_id)] = commit
    _write_last_push(output_dir, data)


def _read_openclaw_cooldown(output_dir: Path) -> Dict[str, float]:
    p = output_dir / OPENCLAW_COOLDOWN_FILE
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in raw.items() if v is not None}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _write_openclaw_cooldown(output_dir: Path, data: Dict[str, float]) -> None:
    p = output_dir / OPENCLAW_COOLDOWN_FILE
    p.write_text(json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")


def _record_openclaw_push_cooldown_now(output_dir: Path, yuque_id: int) -> None:
    d = _read_openclaw_cooldown(output_dir)
    d[str(yuque_id)] = time.time()
    _write_openclaw_cooldown(output_dir, d)


def _openclaw_precall_skip_reason(output_dir: Path, yuque_id: int, diff_text: str) -> Optional[str]:
    """若应跳过对 OpenClaw 的调用则返回原因，否则返回 None。"""
    d = diff_text or ""
    if YUQUE2GIT_OPENCLAW_MIN_DIFF_CHARS > 0 and len(d) < YUQUE2GIT_OPENCLAW_MIN_DIFF_CHARS:
        return f"openclaw_skip_min_diff_chars(len={len(d)},min={YUQUE2GIT_OPENCLAW_MIN_DIFF_CHARS})"
    if YUQUE2GIT_OPENCLAW_COOLDOWN_SECONDS > 0:
        cool = _read_openclaw_cooldown(output_dir)
        last_ts = float(cool.get(str(yuque_id), 0) or 0)
        if last_ts > 0:
            elapsed = time.time() - last_ts
            if elapsed < YUQUE2GIT_OPENCLAW_COOLDOWN_SECONDS:
                bypass = YUQUE2GIT_OPENCLAW_COOLDOWN_BYPASS_CHARS
                if bypass > 0 and len(d) >= bypass:
                    return None
                return (
                    f"openclaw_skip_cooldown(elapsed_s={elapsed:.0f},"
                    f"need_s={YUQUE2GIT_OPENCLAW_COOLDOWN_SECONDS})"
                )
    return None


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
    """解析推送目标列表。支持 YUQUE2GIT_DELIVER_TARGETS JSON 数组，或 CHANNEL+TO（TO 可逗号分隔）。

    OpenClaw Hooks 投递约定（与 Gateway /hooks/agent 一致）：
    - qq：to 为 g:<群号> 或 p:<QQ号>（与 Napcat OneBot 一致）。
    - discord：to 为 channel:<Discord 频道 snowflake> 或 user:<用户 snowflake>（与 openclaw docs 一致）。
    其它 channel 名由 OpenClaw 支持的插件决定。HTTP 直连：qq→Napcat（YUQUE2GIT_DIRECT_SEND_URL），discord→Bot REST（YUQUE2GIT_DISCORD_BOT_TOKEN）；未覆盖的目标走 OpenClaw /hooks/agent。
    """
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


def _direct_send_napcat_available() -> bool:
    return bool(YUQUE2GIT_DIRECT_SEND_URL)


def _direct_send_discord_available() -> bool:
    return bool(YUQUE2GIT_DISCORD_BOT_TOKEN)


def _direct_send_any_backend_configured() -> bool:
    return _direct_send_napcat_available() or _direct_send_discord_available()


def _valid_qq_deliver_to(to: str) -> bool:
    t = (to or "").strip()
    return bool(t.startswith(("g:", "p:")) and t[2:].strip().isdigit())


def _valid_discord_deliver_to(to: str) -> bool:
    t = (to or "").strip()
    if t.startswith("channel:") and t[8:].strip().isdigit():
        return True
    if t.startswith("user:") and t[5:].strip().isdigit():
        return True
    return False


def _partition_deliver_targets(
    targets: List[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """拆成可走 HTTP 直连的目标 vs 需走 OpenClaw Hooks 投递的目标。"""
    direct: List[Dict[str, str]] = []
    gateway: List[Dict[str, str]] = []
    for t in targets:
        ch = (t.get("channel") or "").strip()
        to = (t.get("to") or "").strip()
        if ch == "qq" and _direct_send_napcat_available() and _valid_qq_deliver_to(to):
            direct.append(t)
        elif ch == "discord" and _direct_send_discord_available() and _valid_discord_deliver_to(to):
            direct.append(t)
        else:
            gateway.append(t)
    return direct, gateway


def _split_discord_message_content(text: str, max_chars: int = 2000) -> List[str]:
    """Discord message content 上限 2000 字符，超长拆多条。"""
    if not text:
        return []
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


async def _discord_api_post_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    json_body: Optional[Dict[str, Any]] = None,
) -> bool:
    """POST/PATCH Discord REST；处理 429 retry_after。"""
    for attempt in range(YUQUE2GIT_DELIVER_MAX_RETRIES + 1):
        try:
            r = await client.request(method, url, headers=headers, json=json_body)
        except (httpx.TimeoutException, httpx.RequestError):
            if attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
                await asyncio.sleep(2**attempt)
                continue
            return False
        if r.status_code == 429 and attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
            wait = 1.0
            try:
                data = r.json() if r.content else {}
                if isinstance(data.get("retry_after"), (int, float)):
                    wait = float(data["retry_after"])
            except Exception:
                wait = 2**attempt
            await asyncio.sleep(min(wait, 60.0))
            continue
        if 200 <= r.status_code < 300:
            return True
        if r.status_code >= 500 and attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
            await asyncio.sleep(2**attempt)
            continue
        logger.warning("Discord API %s %s -> %s: %s", method, url, r.status_code, (r.text or "")[:300])
        return False
    return False


async def _send_discord_direct_message(client: httpx.AsyncClient, to: str, message: str) -> bool:
    """Discord Bot REST：to 为 channel:<id> 或 user:<id>（后者先开 DM 再发）。"""
    if not YUQUE2GIT_DISCORD_BOT_TOKEN or not to or not message:
        return False
    to = to.strip()
    token = YUQUE2GIT_DISCORD_BOT_TOKEN.strip()
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    base = YUQUE2GIT_DISCORD_API_BASE
    chunks = _split_discord_message_content(message)

    channel_id: Optional[str] = None
    if to.startswith("channel:"):
        channel_id = to[8:].strip()
    elif to.startswith("user:"):
        uid = to[5:].strip()
        dm_url = f"{base}/users/@me/channels"
        channel_id = ""
        for attempt in range(YUQUE2GIT_DELIVER_MAX_RETRIES + 1):
            try:
                r = await client.post(dm_url, headers=headers, json={"recipient_id": uid})
            except (httpx.TimeoutException, httpx.RequestError) as e:
                if attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
                    await asyncio.sleep(2**attempt)
                    continue
                logger.warning("Discord create DM failed: %s", e)
                return False
            if r.status_code == 429 and attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
                wait = 1.0
                try:
                    data429 = r.json() if r.content else {}
                    if isinstance(data429.get("retry_after"), (int, float)):
                        wait = float(data429["retry_after"])
                except Exception:
                    wait = 2**attempt
                await asyncio.sleep(min(wait, 60.0))
                continue
            if r.status_code >= 400:
                logger.warning("Discord create DM returned %s: %s", r.status_code, (r.text or "")[:200])
                return False
            try:
                data = r.json() if r.content else {}
            except Exception:
                data = {}
            channel_id = str(data.get("id") or "")
            break
        if not channel_id:
            return False
    else:
        return False

    msg_url = f"{base}/channels/{channel_id}/messages"
    for part in chunks:
        ok = await _discord_api_post_with_retry(
            client,
            "POST",
            msg_url,
            headers=headers,
            json_body={"content": part},
        )
        if not ok:
            return False
    return True


async def _send_napcat_message(client: httpx.AsyncClient, channel: str, to: str, message: str) -> bool:
    """直连 Napcat OneBot 11 API 发群消息或私聊。channel=qq，to 为 g:群号 或 p:QQ号。返回是否发送成功。"""
    if not YUQUE2GIT_DIRECT_SEND_URL or channel != "qq" or not to or not message:
        return False
    to = to.strip()
    base = YUQUE2GIT_DIRECT_SEND_URL
    headers = {"Content-Type": "application/json"}
    if YUQUE2GIT_DIRECT_SEND_TOKEN:
        headers["Authorization"] = f"Bearer {YUQUE2GIT_DIRECT_SEND_TOKEN}"
    if to.startswith("g:") and to[2:].strip().isdigit():
        group_id = int(to[2:].strip())
        url = f"{base}/send_group_msg"
        body = {"group_id": group_id, "message": message}
    elif to.startswith("p:") and to[2:].strip().isdigit():
        user_id = int(to[2:].strip())
        url = f"{base}/send_private_msg"
        body = {"user_id": user_id, "message": message}
    else:
        logger.warning("Direct send: unsupported target channel=%r to=%r", channel, to)
        return False
    try:
        r = await client.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            logger.warning("Napcat direct send %s/%s returned %s: %s", channel, to, r.status_code, (r.text or "")[:200])
            return False
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {}
        ok = data.get("status") == "ok" or data.get("retcode") == 0
        if not ok:
            logger.warning("Napcat direct send %s/%s API result: %s", channel, to, data)
        return ok
    except Exception as e:
        logger.warning("Napcat direct send failed: %s", e)
        return False


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
            "push_policy": OPENCLAW_PUSH_POLICY_SHORT,
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
                f"{OPENCLAW_PUSH_POLICY_SHORT}\n\n"
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
    async def _post_with_retry(client: httpx.AsyncClient, b: Dict, channel: str, to: str) -> Tuple[bool, str]:
        """返回 (ok, reason)。"""
        last_reason = "unknown"
        for attempt in range(YUQUE2GIT_DELIVER_MAX_RETRIES + 1):
            try:
                r = await client.post(OPENCLAW_CALLBACK_URL, json=b, headers=headers or None)
            except (httpx.TimeoutException, httpx.RequestError) as e:
                last_reason = f"request_error:{type(e).__name__}"
                if attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
                    wait = 2 ** attempt
                    logger.warning(
                        "OpenClaw POST request error (target %s/%s), retry after %ss (attempt %s/%s): %s",
                        channel, to, wait, attempt + 1, YUQUE2GIT_DELIVER_MAX_RETRIES + 1, e,
                    )
                    await asyncio.sleep(wait)
                    continue
                return False, last_reason

            if r.status_code == 429 or r.status_code >= 500:
                last_reason = f"http_{r.status_code}"
                if attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
                    wait = 2 ** attempt
                    if "Retry-After" in r.headers:
                        try:
                            wait = int(r.headers["Retry-After"])
                        except ValueError:
                            pass
                    logger.warning(
                        "OpenClaw POST %s (target %s/%s), retry after %ss (attempt %s/%s)",
                        r.status_code, channel, to, wait, attempt + 1, YUQUE2GIT_DELIVER_MAX_RETRIES + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning("OpenClaw POST target %s/%s returned %s: %s", channel, to, r.status_code, (r.text or "")[:200])
                return False, last_reason

            if r.status_code >= 400:
                last_reason = f"http_{r.status_code}"
                logger.warning("OpenClaw POST target %s/%s returned %s: %s", channel, to, r.status_code, (r.text or "")[:200])
                return False, last_reason
            return True, "ok"
        return False, last_reason

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for i, b in enumerate(bodies):
                if i > 0 and YUQUE2GIT_DELIVER_DELAY_SECONDS > 0 and len(bodies) > 1:
                    await asyncio.sleep(YUQUE2GIT_DELIVER_DELAY_SECONDS)
                channel = b.get("channel", "")
                to = b.get("to", "")
                if channel or to:
                    logger.info("OpenClaw deliver target %s/%s (request %d/%d)", channel, to, i + 1, len(bodies))
                ok, reason = await _post_with_retry(client, b, channel, to)
                if not ok and OUTPUT_DIR:
                    _append_pending_push_event(
                        OUTPUT_DIR,
                        {
                            "type": "openclaw_callback_delivery_failed",
                            "reason": reason,
                            "target_channel": channel,
                            "target_to": to,
                            "repo_slug": repo_slug,
                            "doc_slug": doc_slug,
                            "yuque_id": yuque_id,
                            "commit": commit,
                            "request_body": b,
                            "next_action": "replay_needed",
                        },
                    )
    except Exception as e:
        logger.warning("OpenClaw callback POST failed: %s", e)
        if OUTPUT_DIR:
            _append_pending_push_event(
                OUTPUT_DIR,
                {
                    "type": "openclaw_callback_exception",
                    "reason": type(e).__name__,
                    "repo_slug": repo_slug,
                    "doc_slug": doc_slug,
                    "yuque_id": yuque_id,
                    "commit": commit,
                    "next_action": "replay_needed",
                },
            )


async def _deliver_openclaw_summary(summary: Dict[str, Any]) -> bool:
    """投递摘要：qq→Napcat（若配 URL）、discord→Discord Bot REST（若配 Token）；其余走 OpenClaw /hooks/agent。"""
    deliver_targets = _parse_deliver_targets()
    if not deliver_targets:
        return False
    message = _format_openclaw_summary(summary)
    direct_targets, gateway_targets = _partition_deliver_targets(deliver_targets)
    all_ok = True

    async def _direct_one(
        client_qq: httpx.AsyncClient,
        client_discord: httpx.AsyncClient,
        ch: str,
        to: str,
    ) -> bool:
        if ch == "qq":
            return await _send_napcat_message(client_qq, ch, to, message)
        if ch == "discord":
            return await _send_discord_direct_message(client_discord, to, message)
        return False

    if direct_targets:
        try:
            # trust_env=False：不继承进程级 HTTP(S)_PROXY，避免语雀/OpenClaw/内网 Napcat 被误走代理
            _dp = YUQUE2GIT_DISCORD_HTTP_PROXY or None
            async with httpx.AsyncClient(timeout=45.0, trust_env=False) as client_qq:
                async with httpx.AsyncClient(
                    timeout=45.0,
                    trust_env=False,
                    proxy=_dp,
                ) as client_discord:
                    for i, t in enumerate(direct_targets):
                        if i > 0 and YUQUE2GIT_DELIVER_DELAY_SECONDS > 0 and len(direct_targets) > 1:
                            await asyncio.sleep(YUQUE2GIT_DELIVER_DELAY_SECONDS)
                        ch, to = t["channel"], t["to"]
                        last_reason = "direct_api_failed"
                        for attempt in range(YUQUE2GIT_DELIVER_MAX_RETRIES + 1):
                            try:
                                ok = await _direct_one(client_qq, client_discord, ch, to)
                            except (httpx.TimeoutException, httpx.RequestError) as e:
                                last_reason = f"request_error:{type(e).__name__}"
                                ok = False
                            if ok:
                                logger.info("Direct send summary delivered to %s/%s", ch, to)
                                break
                            if attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
                                wait = 2**attempt
                                logger.warning(
                                    "Direct send %s/%s failed, retry after %ss (attempt %s/%s)",
                                    ch,
                                    to,
                                    wait,
                                    attempt + 1,
                                    YUQUE2GIT_DELIVER_MAX_RETRIES + 1,
                                )
                                await asyncio.sleep(wait)
                                continue
                            all_ok = False
                            evt_type = (
                                "openclaw_summary_delivery_exception"
                                if last_reason.startswith("request_error")
                                else "openclaw_summary_delivery_failed"
                            )
                            if OUTPUT_DIR:
                                _append_pending_push_event(
                                    OUTPUT_DIR,
                                    {
                                        "type": evt_type,
                                        "reason": last_reason,
                                        "summary": summary,
                                        "target_channel": ch,
                                        "target_to": to,
                                        "next_action": "replay_needed",
                                    },
                                )
        except Exception as e:
            logger.warning("Direct send summary deliver failed: %s", e)
            all_ok = False
            if OUTPUT_DIR:
                _append_pending_push_event(
                    OUTPUT_DIR,
                    {
                        "type": "openclaw_summary_delivery_exception",
                        "reason": type(e).__name__,
                        "summary": summary,
                        "next_action": "replay_needed",
                    },
                )

    if not gateway_targets:
        return all_ok

    if not _is_openclaw_hooks_agent_url(OPENCLAW_CALLBACK_URL):
        logger.warning(
            "Summary deliver: %d target(s) need OpenClaw hooks but OPENCLAW_CALLBACK_URL is not /hooks/agent",
            len(gateway_targets),
        )
        return False

    headers: Dict[str, str] = {}
    if OPENCLAW_HOOKS_TOKEN:
        headers["Authorization"] = f"Bearer {OPENCLAW_HOOKS_TOKEN}"
    bodies = [
        {"message": message, "name": "yuque2git", "deliver": True, "channel": t["channel"], "to": t["to"]}
        for t in gateway_targets
    ]
    gateway_ok = True
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for i, body in enumerate(bodies):
                if i > 0 and YUQUE2GIT_DELIVER_DELAY_SECONDS > 0 and len(bodies) > 1:
                    await asyncio.sleep(YUQUE2GIT_DELIVER_DELAY_SECONDS)
                last_reason = "unknown"
                for attempt in range(YUQUE2GIT_DELIVER_MAX_RETRIES + 1):
                    try:
                        r = await client.post(OPENCLAW_CALLBACK_URL, json=body, headers=headers or None)
                    except (httpx.TimeoutException, httpx.RequestError) as e:
                        last_reason = f"request_error:{type(e).__name__}"
                        if attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
                            wait = 2**attempt
                            logger.warning(
                                "OpenClaw summary deliver retry after %ss (attempt %s/%s): %s",
                                wait,
                                attempt + 1,
                                YUQUE2GIT_DELIVER_MAX_RETRIES + 1,
                                e,
                            )
                            await asyncio.sleep(wait)
                            continue
                        gateway_ok = False
                        if OUTPUT_DIR:
                            _append_pending_push_event(
                                OUTPUT_DIR,
                                {
                                    "type": "openclaw_summary_delivery_exception",
                                    "reason": last_reason,
                                    "summary": summary,
                                    "request_body": body,
                                    "next_action": "replay_needed",
                                },
                            )
                        break
                    if r.status_code == 429 or r.status_code >= 500:
                        last_reason = f"http_{r.status_code}"
                        if attempt < YUQUE2GIT_DELIVER_MAX_RETRIES:
                            wait = 2**attempt
                            logger.warning(
                                "OpenClaw summary deliver %s, retry after %ss (attempt %s/%s)",
                                r.status_code,
                                wait,
                                attempt + 1,
                                YUQUE2GIT_DELIVER_MAX_RETRIES + 1,
                            )
                            await asyncio.sleep(wait)
                            continue
                        gateway_ok = False
                        logger.warning("OpenClaw summary deliver returned %s: %s", r.status_code, (r.text or "")[:200])
                        if OUTPUT_DIR:
                            _append_pending_push_event(
                                OUTPUT_DIR,
                                {
                                    "type": "openclaw_summary_delivery_failed",
                                    "reason": last_reason,
                                    "summary": summary,
                                    "request_body": body,
                                    "next_action": "replay_needed",
                                },
                            )
                        break
                    if r.status_code >= 400:
                        gateway_ok = False
                        logger.warning("OpenClaw summary deliver returned %s: %s", r.status_code, (r.text or "")[:200])
                        if OUTPUT_DIR:
                            _append_pending_push_event(
                                OUTPUT_DIR,
                                {
                                    "type": "openclaw_summary_delivery_failed",
                                    "reason": f"http_{r.status_code}",
                                    "summary": summary,
                                    "request_body": body,
                                    "next_action": "replay_needed",
                                },
                            )
                        break
                    logger.info(
                        "OpenClaw summary delivered to %s/%s (status=%s)",
                        body.get("channel"),
                        body.get("to"),
                        r.status_code,
                    )
                    break
    except Exception as e:
        logger.warning("OpenClaw summary deliver failed: %s", e)
        gateway_ok = False
        if OUTPUT_DIR:
            _append_pending_push_event(
                OUTPUT_DIR,
                {
                    "type": "openclaw_summary_delivery_exception",
                    "reason": type(e).__name__,
                    "summary": summary,
                    "next_action": "replay_needed",
                },
            )
    return all_ok and gateway_ok


# --- FastAPI ---
app = FastAPI(title="yuque2git Webhook")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    """将 /mark-pushed 等接口的 422 校验错误写入日志，便于排查 Agent 回调格式问题。"""
    path = getattr(_request, "url", None) and getattr(_request.url, "path", "") or ""
    if "/mark-pushed" in path:
        logger.warning(
            "mark-pushed 422: request body validation failed. path=%s errors=%s example={\"yuque_id\":261997991,\"commit\":\"<sha>\",\"should_push\":true,\"summary\":{\"title\":\"...\",\"repo_name\":\"...\",\"author\":\"...\",\"doc_url\":\"https://...\",\"highlights\":[\"...\"]}}",
            path,
            exc.errors(),
        )
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/mark-pushed")
async def mark_pushed(body: MarkPushedBody):
    if not OUTPUT_DIR or not OUTPUT_DIR.is_dir():
        return Response(status_code=500, content="OUTPUT_DIR not configured or missing")
    resolved_yuque_id, resolve_source = _resolve_yuque_id_for_mark_pushed(
        OUTPUT_DIR, body.yuque_id, body.repo_slug, body.doc_slug
    )
    if resolved_yuque_id is None:
        logger.warning(
            "mark-pushed rejected: invalid yuque_id=%r (repo_slug=%r, doc_slug=%r). "
            "Expected integer yuque_id or resolvable slug.",
            body.yuque_id,
            body.repo_slug,
            body.doc_slug,
        )
        _append_pending_push_event(
            OUTPUT_DIR,
            {
                "type": "mark_pushed_invalid_yuque_id",
                "raw_yuque_id": body.yuque_id,
                "repo_slug": body.repo_slug,
                "doc_slug": body.doc_slug,
                "commit": body.commit,
                "reason": "unresolvable_yuque_id",
                "next_action": "fix_payload_and_replay",
            },
        )
        return Response(status_code=400, content="invalid yuque_id (expect integer or resolvable slug)")
    if resolve_source not in ("int", "numeric-string"):
        logger.warning(
            "mark-pushed compat-resolve: yuque_id=%r -> %s via %s",
            body.yuque_id,
            resolved_yuque_id,
            resolve_source,
        )

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
        logger.warning(
            "mark-pushed rejected: summary missing or invalid (yuque_id=%s, should_push=true). "
            "summary must be dict with title, repo_name, author, doc_url, highlights (1～3 items).",
            resolved_yuque_id,
        )
        _append_pending_push_event(
            OUTPUT_DIR,
            {
                "type": "mark_pushed_invalid_summary",
                "yuque_id": resolved_yuque_id,
                "commit": body.commit,
                "reason": "summary_missing_or_invalid",
                "raw_summary": body.summary,
                "next_action": "fix_payload_and_replay",
            },
        )
        return Response(status_code=400, content="summary missing or invalid")
    if body.should_push:
        data = _read_last_push(OUTPUT_DIR)
        # 幂等：该 commit 已成功投递过则不再重复 deliver（须在投递成功后再写盘，见下）
        if data.get(str(resolved_yuque_id)) == body.commit:
            return {"ok": True, "yuque_id": resolved_yuque_id, "commit": body.commit, "delivered": False}
        delivered = await _deliver_openclaw_summary(validated_summary)
        if delivered:
            data[str(resolved_yuque_id)] = body.commit
            _write_last_push(OUTPUT_DIR, data)
            if YUQUE2GIT_OPENCLAW_COOLDOWN_SECONDS > 0 and OUTPUT_DIR:
                _record_openclaw_push_cooldown_now(OUTPUT_DIR, resolved_yuque_id)
        return {"ok": True, "yuque_id": resolved_yuque_id, "commit": body.commit, "delivered": delivered}
    return {"ok": True, "yuque_id": resolved_yuque_id, "commit": body.commit, "delivered": False}


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
        author_name = await _resolve_author_name(client, OUTPUT_DIR, detail)
        members = _read_members(OUTPUT_DIR)
        content = _build_md(detail, author_name, members)
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
            skip_reason = _openclaw_precall_skip_reason(OUTPUT_DIR, data.id, diff_text)
            if skip_reason:
                logger.info("OpenClaw skipped (%s) yuque_id=%s", skip_reason, data.id)
                return Response(status_code=200, content="ok")
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
    global OUTPUT_DIR, _WEBHOOK_LOG_FILE
    parser = argparse.ArgumentParser(description="yuque2git Webhook server")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=os.environ.get("OUTPUT_DIR"),
        help="文档存储目录（Git 仓库根），语雀文档将写入此目录下 {repo_slug}/...；可设环境变量 OUTPUT_DIR",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", type=str, default="0.0.0.0")
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Webhook 业务日志文件路径（接收/commit/推送判定），不设则仅 stdout",
    )
    parser.add_argument(
        "--replay-pending",
        action="store_true",
        help="一次性重放 pending 队列后退出，不启动 HTTP 服务",
    )
    parser.add_argument(
        "--replay-limit",
        type=int,
        default=50,
        help="重放时最多处理条数（默认 50），仅与 --replay-pending 同用",
    )
    args = parser.parse_args()
    if not args.output_dir:
        parser.error("请指定 --output-dir 或设置环境变量 OUTPUT_DIR")
    OUTPUT_DIR = Path(args.output_dir).resolve()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.replay_pending:
        done, retry_failed, invalid = asyncio.run(_replay_pending_async(OUTPUT_DIR, args.replay_limit))
        logger.info("replay exit: done=%s retry_failed=%s invalid_payload=%s", done, retry_failed, invalid)
        return

    if args.log_file:
        _WEBHOOK_LOG_FILE = Path(args.log_file).resolve()
        try:
            fh = logging.FileHandler(_WEBHOOK_LOG_FILE, encoding="utf-8")
            fh.setLevel(logging.INFO)
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            logger.addHandler(fh)
            logger.info("Webhook log file: %s", _WEBHOOK_LOG_FILE)
        except OSError as e:
            logger.warning("Cannot open log file %s: %s", _WEBHOOK_LOG_FILE, e)

    uvicorn.run(app, host=args.bind, port=args.port)


if __name__ == "__main__":
    main()
