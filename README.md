# yuque2git

语雀文档同步到本地 Git 仓库：Webhook 实时写 Markdown 并 commit，由 AI 判定是否智能推送并通知订阅者。

- **仓库**：<https://github.com/Gu-Heping/yuque2git>
- **形态**：可作为 OpenClaw 的 Skill 使用，也可独立运行（同一套脚本与配置）。

---

## 快速开始

1. **环境**：建议始终使用项目虚拟环境，避免依赖冲突。在项目根目录下：  
   `python3.11 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt`  
   之后用 `.venv/bin/python` 运行脚本与服务（如 ` .venv/bin/python scripts/sync_to_files.py ...`）。
2. **输出目录**：环境变量 `OUTPUT_DIR`（如 `export OUTPUT_DIR=/data/yuque-docs`）或参数 `--output-dir /path`。目录不存在时 webhook/全量同步会自动创建。
3. **必填配置**：`YUQUE_TOKEN`。
4. **启动 Webhook**：  
   `.venv/bin/python scripts/webhook_server.py --output-dir /path/to/repo --port 8765`
5. **语雀后台**：配置 Webhook URL 指向 `http://your-host:8765/webhook`。
6. **可选**：`scripts/sync_to_files.py` 全量同步；`scripts/sync_toc.py` 同步 TOC（TOC 变更不触发 webhook）。

---

## 输出与行为

- **目录结构**：`{OUTPUT_DIR}/{知识库名}/{父路径}/{文档标题}.md`。根下另有 `.yuque-id-to-path.json`（id→路径）、`.yuque-last-push.json`（上次推送）、`.yuque-members.json`（团队姓名缓存）。
- **智能 diff**：推送判定以前一次推送为基准；文档在语雀中移动父节点时，仍能按旧路径 vs 新路径正确 diff（依赖 `.yuque-id-to-path.json`）。
- **Token 优化**：无实质变更不调 LLM；默认只对正文做 diff（`ENABLE_BODY_ONLY_DIFF=true`）。
- **Markdown**：frontmatter 仅保留 Obsidian 友好字段（id、title、slug、created_at、updated_at、author、book_name、description、cover）；作者优先用团队内姓名（全量同步拉取并缓存）。
- **时间**：`created_at`/`updated_at` 为本地可读时间（`YYYY-MM-DD HH:MM:SS`），时区由 `YUQUE_TIMEZONE` 指定，默认 `Asia/Shanghai`。

---

## 与 OpenClaw 对接

- **模式**：`PUSH_DECISION_MODE=openclaw`，并设置 `OPENCLAW_CALLBACK_URL`（如 `http(s)://<gateway>:<port>/hooks/agent`）、`OPENCLAW_HOOKS_TOKEN`，可选 `YUQUE2GIT_PUBLIC_URL`。
- **投递目标**：`YUQUE2GIT_DELIVER_CHANNEL=qq`、`YUQUE2GIT_DELIVER_TO=<ID>`。多目标时 `YUQUE2GIT_DELIVER_TO` 可逗号分隔（如 `1179350197,g:1087044655`），或使用 `YUQUE2GIT_DELIVER_TARGETS` JSON 数组；服务端会在收到合法结构化摘要后向每个目标单独 POST，两次 POST 间隔由 `YUQUE2GIT_DELIVER_DELAY_SECONDS` 控制（默认 2 秒），避免 rate limit。
- **直连发 QQ（B 方案）**：若希望摘要**不经 Gateway 再跑 Agent**、直接发到 QQ，可设置 `YUQUE2GIT_DIRECT_SEND_URL`（如 `http://127.0.0.1:3000`，即 Napcat OneBot 11 HTTP API 地址）。此时摘要由 yuque2git 直连 Napcat 的 `send_group_msg` / `send_private_msg` 发送，QQ 收到的是 OpenClaw 回调中的摘要原文。可选 `YUQUE2GIT_DIRECT_SEND_TOKEN` 与 Napcat HTTP 服务的 token 一致时用于鉴权。部署时需保证 yuque2git 能访问该端口（本机 3000 或宿主机映射）。
- **429 重试**：若 Gateway/上游返回 429（API rate limit），服务会自动重试（默认最多 3 次、指数退避），可通过环境变量 `YUQUE2GIT_DELIVER_MAX_RETRIES` 调整；仍失败时会在日志中打出 warning。
- **发给 Agent 的 prompt**：含文档标题、知识库、作者、原文地址、本地文件绝对路径（可读以生成概要），并明确要求 Agent 回调结构化 JSON，而不是直接回复最终通知文案；自定义模板见 `YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE`，占位符见 [SKILL.md](SKILL.md)。
- **`/mark-pushed` 回调格式**：至少包含 `yuque_id`、`commit`、`should_push`；当 `should_push=true` 时，必须额外携带 `summary`，其中包含 `title`、`repo_name`、`author`、`doc_url`、`highlights`（1～3 条）。
- **OpenClaw 侧**：在 `openclaw.json` 启用 hooks，并确保 Agent 能回调 `/mark-pushed`；最终发给订阅者的文案由 yuque2git 服务端统一生成与投递。

---

## 回调失败自救

- **标准回调 JSON**（`yuque_id` 必须是整数，不要传 slug）：

```json
{
  "yuque_id": 261997991,
  "commit": "e80935e",
  "should_push": true,
  "summary": {
    "title": "文档标题",
    "repo_name": "知识库名称",
    "author": "作者",
    "doc_url": "https://nova.yuque.com/namespace/repo/doc",
    "highlights": ["更新要点 1", "更新要点 2"]
  }
}
```

- **常见错误**：
  - `422 int_parsing`：`yuque_id` 传成了字符串 slug。
  - `400 summary missing or invalid`：`should_push=true` 但 `summary` 字段不完整或 `highlights` 数量不在 1～3。
- **队列兜底**：失败事件会写入 `OUTPUT_DIR/.yuque-pending-pushes.jsonl`（可通过 `PENDING_PUSH_FILE` 改名），用于重放。
- **手工补发示例**：

```bash
curl -sS -X POST "http://127.0.0.1:8765/mark-pushed" \
  -H "Content-Type: application/json" \
  -d '{
    "yuque_id": 261997991,
    "commit": "e80935e",
    "should_push": true,
    "summary": {
      "title": "文档标题",
      "repo_name": "知识库名称",
      "author": "作者",
      "doc_url": "https://nova.yuque.com/namespace/repo/doc",
      "highlights": ["更新要点 1"]
    }
  }'
```

- **自动重放 pending 队列**（一次性执行后退出，不启动 HTTP 服务）：

```bash
python scripts/webhook_server.py --output-dir /path/to/repo --replay-pending --replay-limit 50
```

  可通过环境变量 `PENDING_PUSH_FILE` 指定 pending 文件名（默认 `.yuque-pending-pushes.jsonl`）；重放使用 `OUTPUT_DIR` 下的 lock 文件避免多实例并发；成功/失败会以追加行（`status=done` / `status=retry_failed` / `status=invalid_payload`）写入同文件。

- **查看 pending 最新 20 条**（排障时确认待重放内容）：

```bash
tail -n 20 /path/to/repo/.yuque-pending-pushes.jsonl
```

  若通过 `PENDING_PUSH_FILE` 改了文件名，请替换为实际路径。

- **排障顺序**：① 确认服务 `/health` 正常、`OUTPUT_DIR` 与 `YUQUE2GIT_PUBLIC_URL` 配置正确；② 用 `tail` 查看 pending 最新条目，区分 `mark_pushed_invalid_*`（可重放至 `/mark-pushed`）与 `openclaw_*_failed`（重试发往 OpenClaw）；③ 执行 `--replay-pending` 自动重放；④ 仍有 4xx 时根据日志修正 payload（如 `yuque_id` 改为整数、`summary` 补全）后再次重放或手工 curl。

---

## 运行测试

```bash
.venv/bin/python -m pytest scripts/tests/ -v
```

覆盖：`_slug_safe`、`_parent_path_from_toc`、`_build_md`、last-push、`_get_diff`、Webhook 解析等，不请求语雀或 LLM。

---

## 项目结构

```
yuque2git/
├── README.md
├── SKILL.md          # OpenClaw Skill 说明
└── scripts/
    ├── requirements.txt
    ├── webhook_server.py   # Webhook 服务
    ├── sync_to_files.py    # 全量同步
    ├── sync_toc.py         # 仅同步 TOC
    └── tests/
        └── test_webhook_server.py
```

---

## License

与上游 yuque-sync-platform 的复用思路一致；本仓库代码可独立使用。
