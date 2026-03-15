# yuque2git

语雀文档同步到本地 Git 仓库：Webhook 实时写 Markdown 并 commit，由 AI 判定是否智能推送并通知订阅者。

- **仓库**：<https://github.com/Gu-Heping/yuque2git>
- **形态**：可作为 OpenClaw 的 Skill 使用，也可独立运行（同一套脚本与配置）。

---

## 快速开始

1. **环境**：项目根目录下  
   `python3.11 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt`  
   之后用 `.venv/bin/python` 运行脚本。
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
- **投递目标**：`YUQUE2GIT_DELIVER_CHANNEL=qq`、`YUQUE2GIT_DELIVER_TO=<ID>`。多目标时 `YUQUE2GIT_DELIVER_TO` 可逗号分隔（如 `1179350197,g:1087044655`），或使用 `YUQUE2GIT_DELIVER_TARGETS` JSON 数组；每个目标会单独 POST，两次 POST 间隔由 `YUQUE2GIT_DELIVER_DELAY_SECONDS` 控制（默认 2 秒），避免 rate limit。
- **发给 Agent 的 prompt**：含文档标题、作者、原文地址、本地文件绝对路径（可读以生成概要）；自定义模板见 `YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE`，占位符见 [SKILL.md](SKILL.md)。
- **OpenClaw 侧**：在 `openclaw.json` 启用 hooks，并确保 Agent 能回调 `/mark-pushed`。

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
