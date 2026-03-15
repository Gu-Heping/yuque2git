# yuque2git

语雀文档同步到本地 Git 仓库：Webhook 实时写 Markdown 并 commit，由 AI 判定是否智能推送并通知订阅者。

- **仓库**：<https://github.com/Gu-Heping/yuque2git>
- **形态**：既可作为 OpenClaw 的 Skill 使用，也可作为独立任务单独运行（同一套脚本与配置）。

## 快速开始

1. **（推荐）使用虚拟环境**：项目根目录下执行  
   `python3.11 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt`  
   之后运行脚本时用 `.venv/bin/python`，例如：  
   `.venv/bin/python scripts/webhook_server.py ...`
2. **指定文档存储位置**：所有脚本统一使用同一目录（建议设为 Git 仓库根）：
   - 环境变量 **`OUTPUT_DIR`**：例如 `export OUTPUT_DIR=/data/yuque-docs`，则 webhook、全量同步、TOC 同步都写入该目录。
   - 或每次用参数 **`--output-dir /path`**。目录不存在时会自动创建（webhook/全量同步）；TOC 同步要求目录已存在。
   - 目录结构：`{OUTPUT_DIR}/{知识库名}/{父路径}/{文档标题}.md`（按知识库名与文档标题便于识别）。根下另有 `.yuque-id-to-path.json`（yuque_id→路径索引）、`.yuque-last-push.json`（上次推送 commit）、`.yuque-members.json`（团队内姓名缓存），由服务与全量同步维护。
3. 配置环境变量：`YUQUE_TOKEN`（必填）。
4. 启动 Webhook 服务：`.venv/bin/python scripts/webhook_server.py --output-dir /path/to/repo --port 8765`（若已设 `OUTPUT_DIR` 可省略 `--output-dir`）
5. 在语雀知识库后台配置 Webhook URL 指向该服务（如 `http://your-host:8765/webhook`）。
6. 可选：运行 `scripts/sync_to_files.py` 做全量同步；运行 `scripts/sync_toc.py` 定期同步 TOC（语雀 TOC 变更不触发 webhook）。二者若不传 `--output-dir` 会使用环境变量 `OUTPUT_DIR`。

**智能 diff**：推送判定以前一次推送为基准做 diff。输出目录内的 `.yuque-id-to-path.json` 记录每篇文档的路径；文档在语雀中移动父节点导致路径变化时，仍能按「旧路径 vs 新路径」正确算 diff。**Token 优化**：无实质变更时不调 LLM；默认只对正文做 diff（`ENABLE_BODY_ONLY_DIFF=true`），减少 frontmatter 噪音与消耗。**Markdown 与作者**：写入的 frontmatter 仅保留 Obsidian 友好字段（id、title、slug、created_at、updated_at、author、book_name、description、cover）；作者名优先使用团队内姓名（全量同步时拉取 `/groups/{id}/statistics/members` 写入 `.yuque-members.json` 缓存）。

**与 OpenClaw 对接**：若使用 `PUSH_DECISION_MODE=openclaw`，将 `OPENCLAW_CALLBACK_URL` 设为 OpenClaw Gateway 的 `http(s)://<gateway>:<port>/hooks/agent`，并配置 `OPENCLAW_HOOKS_TOKEN`、可选 `YUQUE2GIT_PUBLIC_URL`。投递到 QQ：设置 `YUQUE2GIT_DELIVER_CHANNEL=qq` 与 `YUQUE2GIT_DELIVER_TO=<ID>`；**多目标**时 `YUQUE2GIT_DELIVER_TO` 可逗号分隔（如 `1179350197,g:1087044655`），或使用 `YUQUE2GIT_DELIVER_TARGETS` JSON 数组，每个目标会单独发一次 POST 确保群与私聊都收到。发给 Agent 的 prompt 含文档标题、作者（团队内姓名）、原文地址、**本地文件绝对路径**（Agent 可直接读取以生成概要）；自定义 prompt 可用 `YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE`，占位符见 [SKILL.md](SKILL.md)。OpenClaw 侧需在 `openclaw.json` 启用 hooks 并确保 Agent 能回调 `/mark-pushed`。

## 运行测试

在项目根目录执行（需先激活 venv 或使用 `.venv/bin/python`）：

```bash
.venv/bin/python -m pytest scripts/tests/ -v
```

测试覆盖：`_slug_safe`、`_parent_path_from_toc`、`_build_md`、last-push 读写、`_get_diff`、Webhook payload 解析等，不请求真实语雀或 LLM。

## 目录结构

```
yuque2git/
├── README.md
├── SKILL.md          # OpenClaw Skill 说明
└── scripts/
    ├── requirements.txt
    ├── webhook_server.py   # Webhook 服务（接收语雀推送、写文件、commit、智能推送）
    ├── sync_to_files.py    # 全量同步
    ├── sync_toc.py         # 仅同步 TOC
    └── tests/
        └── test_webhook_server.py  # 单元测试
```

## License

与上游 yuque-sync-platform 的复用思路一致；本仓库代码可独立使用。
