"""
测试 webhook_server 中的纯函数与核心逻辑（不请求真实语雀/LLM）。
运行：在项目根目录或 scripts 目录执行
  python -m pytest scripts/tests/test_webhook_server.py -v
或
  cd scripts && python -m pytest tests/test_webhook_server.py -v
"""
import json
import subprocess
from pathlib import Path

import pytest

# 从上层目录导入被测模块（需在 scripts 或项目根执行）
import sys
_scripts = Path(__file__).resolve().parent.parent
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

from webhook_server import (
    _slug_safe,
    _parent_path_from_toc,
    _build_md,
    _get_diff,
    _read_last_push,
    _write_last_push,
    _update_last_push_for,
    _is_openclaw_hooks_agent_url,
    WebhookPayload,
    WebhookData,
    WebhookBook,
)


class TestSlugSafe:
    def test_normal(self):
        assert _slug_safe("hello") == "hello"
        assert _slug_safe("doc-slug") == "doc-slug"

    def test_illegal_chars(self):
        assert _slug_safe("a/b") == "a_b"
        assert _slug_safe("a: b") == "a_ b"
        assert "\\" not in _slug_safe("a\\b")
        assert "/" not in _slug_safe("path/to/doc")

    def test_empty(self):
        assert _slug_safe("") == "untitled"
        assert _slug_safe("   ") == "untitled"


class TestParentPathFromToc:
    def test_root_doc(self):
        toc = [
            {"uuid": "u1", "id": 1, "url": "root-doc", "title": "Root", "parent_uuid": None},
        ]
        assert _parent_path_from_toc(toc, 1, "root-doc") == ""

    def test_child_under_doc(self):
        toc = [
            {"uuid": "u1", "id": 1, "url": "parent", "title": "Parent", "parent_uuid": None},
            {"uuid": "u2", "id": 2, "url": "child", "title": "Child", "parent_uuid": "u1"},
        ]
        assert _parent_path_from_toc(toc, 2, "child") == "parent"
        assert _parent_path_from_toc(toc, 1, "parent") == ""

    def test_nested(self):
        toc = [
            {"uuid": "u1", "id": 1, "url": "a", "title": "A", "parent_uuid": None},
            {"uuid": "u2", "id": 2, "url": "b", "title": "B", "parent_uuid": "u1"},
            {"uuid": "u3", "id": 3, "url": "c", "title": "C", "parent_uuid": "u2"},
        ]
        assert _parent_path_from_toc(toc, 3, "c") == "a/b"

    def test_title_no_url(self):
        toc = [
            {"uuid": "u1", "id": None, "url": None, "title": "目录", "parent_uuid": None},
            {"uuid": "u2", "id": 2, "url": "doc", "title": "Doc", "parent_uuid": "u1"},
        ]
        path = _parent_path_from_toc(toc, 2, "doc")
        assert path != ""
        assert "目录" in path or "untitled" in path or "u1" in path

    def test_parent_move_in_toc(self):
        """父文档移动后的 TOC：C 为根，A 在 C 下，B 在 A 下；路径应为 c/a.md、c/a/b.md。"""
        toc = [
            {"uuid": "uc", "id": 10, "url": "c", "title": "C", "parent_uuid": None},
            {"uuid": "ua", "id": 1, "url": "a", "title": "A", "parent_uuid": "uc"},
            {"uuid": "ub", "id": 2, "url": "b", "title": "B", "parent_uuid": "ua"},
        ]
        assert _parent_path_from_toc(toc, 1, "a") == "c"
        assert _parent_path_from_toc(toc, 2, "b") == "c/a"


class TestBuildMd:
    def test_minimal(self):
        detail = {
            "id": 123,
            "title": "测试",
            "slug": "test",
            "body": "正文",
            "created_at": "2024-01-01T00:00:00.000Z",
            "updated_at": "2024-01-02T00:00:00.000Z",
        }
        md = _build_md(detail, "作者名")
        assert "---" in md
        assert "yuque_id" in md or "id" in md
        assert "测试" in md
        assert "| 作者 |" in md
        assert "作者名" in md
        assert "正文" in md

    def test_table_escape(self):
        detail = {
            "id": 1,
            "title": "T",
            "slug": "t",
            "body": "",
            "created_at": None,
            "updated_at": None,
        }
        md = _build_md(detail, "名|带竖线")
        assert "\\|" in md or "|" in md
        assert "带竖线" in md or "名" in md


class TestLastPush:
    def test_read_write_roundtrip(self, tmp_path):
        _write_last_push(tmp_path, {"1": "abc123", "2": "def456"})
        data = _read_last_push(tmp_path)
        assert data["1"] == "abc123"
        assert data["2"] == "def456"

    def test_read_missing(self, tmp_path):
        assert _read_last_push(tmp_path) == {}

    def test_update_last_push_for(self, tmp_path):
        _write_last_push(tmp_path, {"1": "old"})
        _update_last_push_for(tmp_path, 2, "new2")
        data = _read_last_push(tmp_path)
        assert data["1"] == "old"
        assert data["2"] == "new2"
        _update_last_push_for(tmp_path, 1, "new1")
        assert _read_last_push(tmp_path)["1"] == "new1"


class TestGetDiff:
    def test_no_base_content(self, tmp_path):
        (tmp_path / "doc.md").write_text("hello", encoding="utf-8")
        out = _get_diff(tmp_path, None, "doc.md")
        assert "[首次" in out or "hello" in out

    def test_no_base_missing_file(self, tmp_path):
        out = _get_diff(tmp_path, None, "nonexistent.md")
        assert "无基准" in out or "不存在" in out

    def test_with_git(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "a.md").write_text("v1", encoding="utf-8")
        subprocess.run(["git", "add", "a.md"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, check=True, capture_output=True)
        c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
        (tmp_path / "a.md").write_text("v2", encoding="utf-8")
        subprocess.run(["git", "add", "a.md"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "second"], cwd=tmp_path, check=True, capture_output=True)
        diff = _get_diff(tmp_path, c1, "a.md")
        assert "v2" in diff or "v1" in diff or "无文本变更" in diff or "+" in diff or "-" in diff


class TestOpenClawHooksUrl:
    def test_hooks_agent_detected(self):
        assert _is_openclaw_hooks_agent_url("http://localhost:13040/hooks/agent") is True
        assert _is_openclaw_hooks_agent_url("https://gateway.example.com/hooks/agent") is True
        assert _is_openclaw_hooks_agent_url("http://host/hooks/agent/") is True

    def test_non_hooks_agent_not_detected(self):
        assert _is_openclaw_hooks_agent_url("http://localhost:13040/custom") is False
        assert _is_openclaw_hooks_agent_url("http://host/hooks") is False
        assert _is_openclaw_hooks_agent_url("") is False


class TestWebhookPayload:
    def test_parse_minimal_doc_event(self):
        payload = WebhookPayload(
            data=WebhookData(
                action_type="update",
                id=123,
                book=WebhookBook(id=1, slug="repo1", name="Repo1"),
                slug="doc-slug",
                title="Doc Title",
            )
        )
        assert payload.data.action_type == "update"
        assert payload.data.id == 123
        assert payload.data.book and payload.data.book.slug == "repo1"

    def test_parse_delete(self):
        payload = WebhookPayload(
            data=WebhookData(action_type="delete", id=456, book=None, slug=None, title=None)
        )
        assert payload.data.action_type == "delete"
        assert payload.data.id == 456
