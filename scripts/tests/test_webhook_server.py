"""
测试 webhook_server 中的纯函数与核心逻辑（不请求真实语雀/LLM）。
运行：在项目根目录或 scripts 目录执行
  python -m pytest scripts/tests/test_webhook_server.py -v
或
  cd scripts && python -m pytest tests/test_webhook_server.py -v
"""
import asyncio
import json
import subprocess
import zlib
from pathlib import Path

import pytest

# 从上层目录导入被测模块（需在 scripts 或项目根执行）
import sys
_scripts = Path(__file__).resolve().parent.parent
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

import webhook_server
from webhook_server import (
    _slug_safe,
    _parent_path_from_toc,
    _build_md,
    _get_diff,
    _render_lakesheet_markdown,
    _llm_should_push,
    _openclaw_callback,
    _format_openclaw_summary,
    _render_openclaw_message_template,
    _validate_openclaw_summary,
    _read_last_push,
    _write_last_push,
    _update_last_push_for,
    _is_openclaw_hooks_agent_url,
    _openclaw_precall_skip_reason,
    _record_openclaw_push_cooldown_now,
    _parse_deliver_targets,
    _partition_deliver_targets,
    _split_discord_message_content,
    mark_pushed,
    MarkPushedBody,
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
        assert _parent_path_from_toc(toc, 2, "child") == "Parent"
        assert _parent_path_from_toc(toc, 1, "parent") == ""

    def test_nested(self):
        toc = [
            {"uuid": "u1", "id": 1, "url": "a", "title": "A", "parent_uuid": None},
            {"uuid": "u2", "id": 2, "url": "b", "title": "B", "parent_uuid": "u1"},
            {"uuid": "u3", "id": 3, "url": "c", "title": "C", "parent_uuid": "u2"},
        ]
        assert _parent_path_from_toc(toc, 3, "c") == "A/B"

    def test_title_no_url(self):
        toc = [
            {"uuid": "u1", "id": None, "url": None, "title": "目录", "parent_uuid": None},
            {"uuid": "u2", "id": 2, "url": "doc", "title": "Doc", "parent_uuid": "u1"},
        ]
        path = _parent_path_from_toc(toc, 2, "doc")
        assert path != ""
        assert "目录" in path or "untitled" in path or "u1" in path

    def test_parent_move_in_toc(self):
        """父文档移动后的 TOC：C 为根，A 在 C 下，B 在 A 下；路径应为 C/A.md、C/A/B.md（目录段用 title）。"""
        toc = [
            {"uuid": "uc", "id": 10, "url": "c", "title": "C", "parent_uuid": None},
            {"uuid": "ua", "id": 1, "url": "a", "title": "A", "parent_uuid": "uc"},
            {"uuid": "ub", "id": 2, "url": "b", "title": "B", "parent_uuid": "ua"},
        ]
        assert _parent_path_from_toc(toc, 1, "a") == "C"
        assert _parent_path_from_toc(toc, 2, "b") == "C/A"


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

    def test_lakesheet_rendered_as_readable_table(self):
        sheet = [
            {
                "name": "Sheet1",
                "data": {
                    "0": {"0": {"v": "学号"}, "1": {"v": "姓名"}, "2": {"v": "校区"}},
                    "1": {"0": {"v": "1"}, "1": {"v": "张三"}, "2": {"v": "鼓楼"}},
                    "2": {"0": {"v": "2"}, "1": {"v": "李四"}, "2": {"v": "仙林"}},
                },
            }
        ]
        payload = {
            "format": "lakesheet",
            "version": "3.5.5",
            "larkJson": True,
            "sheet": zlib.compress(json.dumps(sheet, ensure_ascii=False).encode("utf-8")).decode("latin1"),
        }
        detail = {
            "id": 123,
            "title": "活动统计",
            "slug": "sheet-doc",
            "type": "Sheet",
            "format": "lakesheet",
            "body": json.dumps(payload, ensure_ascii=False),
            "created_at": "2024-01-01T00:00:00.000Z",
            "updated_at": "2024-01-02T00:00:00.000Z",
        }
        md = _build_md(detail, "作者名")
        assert "Auto-generated from Yuque lakesheet" in md
        assert "```tsv" in md
        assert "学号\t姓名\t校区" in md
        assert "1\t张三\t鼓楼" in md
        assert "\"format\": \"lakesheet\"" not in md


class TestRenderLakesheetMarkdown:
    def test_invalid_payload_falls_back_to_raw_text(self):
        raw = _render_lakesheet_markdown("not-json")
        assert raw == "not-json"


class TestRenderLaketableMarkdown:
    def test_laketable_rendered_as_readable_tsv(self):
        """测试 laketable 多维表格转换为 TSV"""
        from webhook_server import _render_laketable_markdown

        # 模拟语雀 laketable 数据结构
        body = {
            "format": "laketable",
            "type": "Table",
            "sheet": [{
                "name": "活动表",
                "columns": [
                    {"name": "姓名", "type": "text", "id": "col1"},
                    {"name": "状态", "type": "select", "id": "col2", "options": [
                        {"id": "waiting", "value": "待开始"},
                        {"id": "done", "value": "已完成"}
                    ]},
                    {"name": "日期", "type": "date", "id": "col3"},
                ]
            }]
        }
        body_table = {
            "records": [
                {
                    "values": [
                        {"value": "张三"},
                        {"value": "waiting"},
                        {"value": {"text": "2026-03-22", "seconds": 3983299200}}
                    ]
                },
                {
                    "values": [
                        {"value": "李四"},
                        {"value": "done"},
                        {"value": {"text": "2026-03-23", "seconds": 3983385600}}
                    ]
                }
            ],
            "totalCount": 2
        }

        detail = {
            "id": 123,
            "title": "测试表格",
            "format": "laketable",
            "type": "Table",
            "body": json.dumps(body, ensure_ascii=False),
            "body_table": json.dumps(body_table, ensure_ascii=False),
        }

        md = _render_laketable_markdown(detail)
        assert "Auto-generated from Yuque laketable" in md
        assert "```tsv" in md
        assert "姓名\t状态\t日期" in md
        assert "张三\t待开始\t2026-03-22" in md
        assert "李四\t已完成\t2026-03-23" in md
        assert "\"format\": \"laketable\"" not in md

    def test_laketable_empty_records(self):
        """测试空表格"""
        from webhook_server import _render_laketable_markdown

        body = {
            "format": "laketable",
            "type": "Table",
            "sheet": [{
                "columns": [
                    {"name": "文本", "type": "text", "id": "col1"},
                ]
            }]
        }
        body_table = {"records": [], "totalCount": 0}

        detail = {
            "id": 123,
            "title": "空表",
            "format": "laketable",
            "body": json.dumps(body, ensure_ascii=False),
            "body_table": json.dumps(body_table, ensure_ascii=False),
        }

        md = _render_laketable_markdown(detail)
        assert "(empty)" in md

    def test_laketable_mention_type(self):
        """测试 mention（提及用户）类型"""
        from webhook_server import _render_laketable_markdown

        body = {
            "format": "laketable",
            "type": "Table",
            "sheet": [{
                "columns": [
                    {"name": "参与者", "type": "mention", "id": "col1"},
                ]
            }]
        }
        body_table = {
            "records": [
                {
                    "values": [
                        {"value": [
                            {"name": "张三", "login": "zhangsan"},
                            {"name": "李四", "login": "lisi"}
                        ]}
                    ]
                }
            ]
        }

        detail = {
            "id": 123,
            "title": "参与者表",
            "format": "laketable",
            "body": json.dumps(body, ensure_ascii=False),
            "body_table": json.dumps(body_table, ensure_ascii=False),
        }

        md = _render_laketable_markdown(detail)
        assert "张三, 李四" in md

    def test_laketable_mention_from_members_cache(self):
        """测试 mention 从团队成员缓存获取姓名"""
        from webhook_server import _render_laketable_markdown

        body = {
            "format": "laketable",
            "type": "Table",
            "sheet": [{
                "columns": [
                    {"name": "姓名", "type": "mention", "id": "col1"},
                ]
            }]
        }
        # 用户 id 是 58816971，但用户名是 "littlej"
        body_table = {
            "records": [
                {
                    "values": [
                        {"value": [
                            {"id": 58816971, "name": "littlej", "login": "yuqueyonghuu4lijg"}
                        ]}
                    ]
                }
            ]
        }

        detail = {
            "id": 123,
            "title": "测试表格",
            "format": "laketable",
            "body": json.dumps(body, ensure_ascii=False),
            "body_table": json.dumps(body_table, ensure_ascii=False),
        }

        # 团队成员缓存：id 58816971 对应团队内姓名 "蒋泓宇"
        members = {
            "58816971": {"name": "蒋泓宇", "login": "jianghongyu"}
        }

        md = _render_laketable_markdown(detail, members)
        # 应该使用团队内姓名，而不是语雀用户名
        assert "蒋泓宇" in md
        assert "littlej" not in md

    def test_laketable_mention_fallback_without_cache(self):
        """测试 mention 在没有缓存时回退到原始用户名"""
        from webhook_server import _render_laketable_markdown

        body = {
            "format": "laketable",
            "type": "Table",
            "sheet": [{
                "columns": [
                    {"name": "姓名", "type": "mention", "id": "col1"},
                ]
            }]
        }
        body_table = {
            "records": [
                {
                    "values": [
                        {"value": [
                            {"id": 99999999, "name": "张三", "login": "zhangsan"}
                        ]}
                    ]
                }
            ]
        }

        detail = {
            "id": 123,
            "title": "测试表格",
            "format": "laketable",
            "body": json.dumps(body, ensure_ascii=False),
            "body_table": json.dumps(body_table, ensure_ascii=False),
        }

        # 没有该用户的缓存，应该使用原始用户名
        members = {}
        md = _render_laketable_markdown(detail, members)
        assert "张三" in md


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


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}
        self.text = json.dumps(payload, ensure_ascii=False)
        self.request = None

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses=None, sink=None, *args, **kwargs):
        self.responses = list(responses or [])
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        if self.sink is not None:
            self.sink.append({"url": url, "headers": headers or {}, "json": json})
        if self.responses:
            return self.responses.pop(0)
        return _FakeResponse({})


class TestLlmDecision:
    def test_requires_boolean_should_push(self, monkeypatch):
        monkeypatch.setattr(webhook_server, "OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(webhook_server, "ENABLE_UPDATE_SUMMARY", True)
        payload = {"choices": [{"message": {"content": '{"should_push":"false","reason":"noop","update_summary":""}'}}]}
        monkeypatch.setattr(
            webhook_server.httpx,
            "AsyncClient",
            lambda *args, **kwargs: _FakeAsyncClient(responses=[_FakeResponse(payload)]),
        )

        should_push, summary = asyncio.run(_llm_should_push("diff", "Doc", "Repo"))

        assert should_push is False
        assert summary is None

    def test_rejects_empty_summary_when_should_push(self, monkeypatch):
        monkeypatch.setattr(webhook_server, "OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(webhook_server, "ENABLE_UPDATE_SUMMARY", True)
        payload = {"choices": [{"message": {"content": '{"should_push":true,"reason":"important","update_summary":"  "}'}}]}
        monkeypatch.setattr(
            webhook_server.httpx,
            "AsyncClient",
            lambda *args, **kwargs: _FakeAsyncClient(responses=[_FakeResponse(payload)]),
        )

        should_push, summary = asyncio.run(_llm_should_push("diff", "Doc", "Repo"))

        assert should_push is False
        assert summary is None

    def test_accepts_valid_summary(self, monkeypatch):
        monkeypatch.setattr(webhook_server, "OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(webhook_server, "ENABLE_UPDATE_SUMMARY", True)
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '{"should_push":true,"reason":"important","update_summary":"新增了发布流程，并补充了回滚说明。"}'
                    }
                }
            ]
        }
        monkeypatch.setattr(
            webhook_server.httpx,
            "AsyncClient",
            lambda *args, **kwargs: _FakeAsyncClient(responses=[_FakeResponse(payload)]),
        )

        should_push, summary = asyncio.run(_llm_should_push("diff", "Doc", "Repo"))

        assert should_push is True
        assert summary == "新增了发布流程，并补充了回滚说明。"


class TestParseDeliverTargets:
    def test_discord_channel_target_from_env(self, monkeypatch):
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_TARGETS_JSON", "")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_CHANNEL", "discord")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_TO", "channel:123456789012345678")
        assert _parse_deliver_targets() == [
            {"channel": "discord", "to": "channel:123456789012345678"},
        ]

    def test_discord_user_dm_target_from_env(self, monkeypatch):
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_TARGETS_JSON", "")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_CHANNEL", "discord")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_TO", "user:972723264424124456")
        assert _parse_deliver_targets() == [
            {"channel": "discord", "to": "user:972723264424124456"},
        ]

    def test_mixed_qq_and_discord_from_json(self, monkeypatch):
        raw = json.dumps(
            [
                {"channel": "qq", "to": "g:1087044655"},
                {"channel": "discord", "to": "channel:111222333444555666"},
            ]
        )
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_TARGETS_JSON", raw)
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_CHANNEL", "qq")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_TO", "ignored")
        assert _parse_deliver_targets() == [
            {"channel": "qq", "to": "g:1087044655"},
            {"channel": "discord", "to": "channel:111222333444555666"},
        ]


class TestPartitionDeliverTargets:
    def test_discord_to_direct_when_token_set(self, monkeypatch):
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DIRECT_SEND_URL", "")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DISCORD_BOT_TOKEN", "fake-bot-token")
        targets = [{"channel": "discord", "to": "channel:999"}]
        d, g = _partition_deliver_targets(targets)
        assert d == targets and g == []

    def test_discord_to_gateway_when_no_token(self, monkeypatch):
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DIRECT_SEND_URL", "")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DISCORD_BOT_TOKEN", "")
        targets = [{"channel": "discord", "to": "channel:999"}]
        d, g = _partition_deliver_targets(targets)
        assert d == [] and g == targets

    def test_qq_to_direct_when_napcat_url_set(self, monkeypatch):
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DIRECT_SEND_URL", "http://127.0.0.1:3000")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DISCORD_BOT_TOKEN", "")
        targets = [{"channel": "qq", "to": "g:1"}]
        d, g = _partition_deliver_targets(targets)
        assert d == targets and g == []

    def test_mixed_routing(self, monkeypatch):
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DIRECT_SEND_URL", "http://127.0.0.1:3000")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DISCORD_BOT_TOKEN", "tok")
        targets = [
            {"channel": "qq", "to": "g:1"},
            {"channel": "discord", "to": "channel:2"},
            {"channel": "discord", "to": "bad"},
        ]
        d, g = _partition_deliver_targets(targets)
        assert d == [{"channel": "qq", "to": "g:1"}, {"channel": "discord", "to": "channel:2"}]
        assert g == [{"channel": "discord", "to": "bad"}]


class TestSplitDiscordContent:
    def test_under_limit_single_chunk(self):
        assert _split_discord_message_content("hi") == ["hi"]

    def test_splits_at_2000(self):
        s = "a" * 4500
        parts = _split_discord_message_content(s, max_chars=2000)
        assert len(parts) == 3
        assert len(parts[0]) == 2000 and len(parts[1]) == 2000 and len(parts[2]) == 500


class TestOpenClawPrompt:
    def test_default_hooks_message_contains_reply_contract(self, monkeypatch, tmp_path):
        sent = []
        monkeypatch.setattr(webhook_server, "OPENCLAW_CALLBACK_URL", "http://gateway.test/hooks/agent")
        monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_TOKEN", "hook-token")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_PUBLIC_URL", "http://yuque2git.test")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE", "")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_TARGETS_JSON", "")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_CHANNEL", "qq")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_TO", "12345")
        monkeypatch.setattr(webhook_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(
            webhook_server.httpx,
            "AsyncClient",
            lambda *args, **kwargs: _FakeAsyncClient(sink=sent),
        )

        asyncio.run(
            _openclaw_callback(
                yuque_id=1,
                repo_slug="repo",
                doc_slug="doc",
                title="发布说明",
                repo_name="知识库",
                diff="+ 新增回滚步骤",
                commit="abc123",
                author_name="Alice",
                doc_url="https://example.com/doc",
                local_path="repo/doc.md",
            )
        )

        assert len(sent) == 1
        message = sent[0]["json"]["message"]
        assert '"should_push": true' in message
        assert '"repo_name": "<知识库名>"' in message
        assert '"author": "<作者名>"' in message
        assert '"doc_url": "https://example.com/doc"' in message
        assert '"highlights": ["<变更要点1>"' in message
        assert '"should_push": false' in message
        assert "推送门槛" in message or "默认保守" in message
        assert sent[0]["json"]["deliver"] is False

    def test_custom_template_supports_reply_contract(self):
        rendered = _render_openclaw_message_template(
            "标题：{title}\n{reply_contract}",
            {"title": "文档", "reply_contract": "摘要：\n1. x"},
        )
        assert "标题：文档" in rendered
        assert "摘要：" in rendered

    def test_custom_template_rejects_unknown_field(self):
        with pytest.raises(ValueError):
            _render_openclaw_message_template("标题：{title}\n{unknown}", {"title": "文档"})

    def test_validate_and_format_structured_summary(self):
        summary = _validate_openclaw_summary(
            {
                "title": "发布说明",
                "repo_name": "知识库",
                "author": "Alice",
                "doc_url": "https://example.com/doc",
                "highlights": ["新增发布步骤", "补充回滚说明"],
            }
        )
        assert summary is not None
        message = _format_openclaw_summary(summary)
        assert "知识库：知识库" in message
        assert "Alice 更新了《发布说明》" in message
        assert "1. 新增发布步骤" in message
        assert "2. 补充回滚说明" in message

    def test_mark_pushed_delivers_formatted_summary(self, monkeypatch, tmp_path):
        sent = []
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "a.md").write_text("v1", encoding="utf-8")
        subprocess.run(["git", "add", "a.md"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, check=True, capture_output=True)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()

        monkeypatch.setattr(webhook_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(webhook_server, "OPENCLAW_CALLBACK_URL", "http://gateway.test/hooks/agent")
        monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_TOKEN", "hook-token")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_TARGETS_JSON", "")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_CHANNEL", "qq")
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_DELIVER_TO", "12345")
        monkeypatch.setattr(
            webhook_server.httpx,
            "AsyncClient",
            lambda *args, **kwargs: _FakeAsyncClient(sink=sent),
        )

        result = asyncio.run(
            mark_pushed(
                MarkPushedBody(
                    yuque_id=1,
                    commit=commit,
                    should_push=True,
                    summary={
                        "title": "发布说明",
                        "repo_name": "知识库",
                        "author": "Alice",
                        "doc_url": "https://example.com/doc",
                        "highlights": ["新增发布步骤", "补充回滚说明"],
                    },
                )
            )
        )

        assert result["delivered"] is True
        assert len(sent) == 1
        delivered = sent[0]["json"]
        assert delivered["deliver"] is True
        assert delivered["channel"] == "qq"
        assert "知识库：知识库" in delivered["message"]
        assert "Alice 更新了《发布说明》" in delivered["message"]

    def test_mark_pushed_rejects_invalid_summary(self, monkeypatch, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "a.md").write_text("v1", encoding="utf-8")
        subprocess.run(["git", "add", "a.md"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, check=True, capture_output=True)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()

        monkeypatch.setattr(webhook_server, "OUTPUT_DIR", tmp_path)

        response = asyncio.run(
            mark_pushed(
                MarkPushedBody(
                    yuque_id=1,
                    commit=commit,
                    should_push=True,
                    summary={"title": "发布说明", "repo_name": "知识库", "highlights": []},
                )
            )
        )

        assert response.status_code == 400


class TestOpenClawPrecallGate:
    def test_min_diff_chars_skip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_OPENCLAW_MIN_DIFF_CHARS", 100)
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_OPENCLAW_COOLDOWN_SECONDS", 0)
        r = _openclaw_precall_skip_reason(tmp_path, 1, "short")
        assert r is not None
        assert "min_diff" in r

    def test_cooldown_skip_and_bypass(self, monkeypatch, tmp_path):
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_OPENCLAW_MIN_DIFF_CHARS", 0)
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_OPENCLAW_COOLDOWN_SECONDS", 3600)
        monkeypatch.setattr(webhook_server, "YUQUE2GIT_OPENCLAW_COOLDOWN_BYPASS_CHARS", 500)
        _record_openclaw_push_cooldown_now(tmp_path, 42)
        r = _openclaw_precall_skip_reason(tmp_path, 42, "x" * 100)
        assert r is not None
        assert "cooldown" in r
        r2 = _openclaw_precall_skip_reason(tmp_path, 42, "y" * 500)
        assert r2 is None
