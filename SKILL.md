---
metadata: '{"openclaw":{"requires":{"bins":["python3"],"env":["YUQUE_TOKEN"]},"homepage":"https://github.com/Gu-Heping/yuque2git"}}'
---

# yuque2git

**Description**：将语雀文档同步到本地文件系统并纳入 Git 版本管理。通过 Webhook 接收 publish/update/delete，把文档写成带 YAML frontmatter 的 Markdown，按 TOC 层级建目录，每次变更自动 commit。支持**智能推送**：由本机 LLM 或 OpenClaw 根据 diff 判定是否推送并生成变更摘要通知订阅者，适合知识库的本地镜像与协作发布。

**触发词**：语雀 webhook、yuque 同步到文件、知识库导出到本地、yuque2git。

---

## 安装（Install）

将本 Skill 安装到 OpenClaw 工作区后，Agent 可按本文档操作语雀同步与智能推送；运行 Webhook 与同步脚本时请使用**本仓库所在目录**（见下方「测试与部署」）。

1. **放入 OpenClaw workspace 的 skills 目录**（二选一）：
   - **从 GitHub 克隆**：`cd ~/.openclaw/workspace/skills && git clone https://github.com/Gu-Heping/yuque2git.git`
   - **符号链接**（本机已有仓库时）：`ln -snf /path/to/yuque2git ~/.openclaw/workspace/skills/yuque2git`
2. **启用**：在 `~/.openclaw/openclaw.json` 的 `skills.entries` 中加入 `"yuque2git": { "enabled": true }`。
3. **配置与运行**：在**本仓库根目录**配置 `.env`（至少 `YUQUE_TOKEN`、`OUTPUT_DIR`），并从此目录启动 Webhook 服务（见「使用方式」）。不要从 workspace 内克隆副本的目录直接运行服务，以免路径与行为不一致。

---

## 何时使用

- 需要把语雀知识库**实时同步到本地 Markdown 文件**，且希望每次变更都**写入 Git** 便于追溯。
- 希望根据**变更内容**由 AI 判定是否推送/通知，减少噪音（智能推送）。
- 既可当作 **OpenClaw 的 Skill**（Agent 按本文档操作、可选 OpenClaw 模式推送），也可当作**独立任务**单独运行（仅脚本 + 服务 + LLM 模式）。

## 前置条件

- **YUQUE_TOKEN**：语雀 API Token（个人或团队），必填。
- 语雀知识库需在后台配置 **Webhook 地址**，指向本服务的 `POST /webhook`（或 `/yuque`）。

## 配置项

| 环境变量 | 说明 |
|----------|------|
| `YUQUE_TOKEN` | 语雀 API Token，必填 |
| `YUQUE_BASE_URL` | 语雀 API 基地址，可选，默认 `https://nova.yuque.com/api/v2` |
| **全量同步限流**（`sync_to_files.py`） | 降低触发语雀 429 的概率 |
| `YUQUE_SYNC_CONCURRENCY` | 并发请求数，默认 3 |
| `YUQUE_SYNC_REQUEST_DELAY` | 每次请求成功后的间隔秒数，默认 0.25 |
| `YUQUE_SYNC_MAX_RETRIES` | 遇 429/5xx 时最大重试次数，默认 4；会按 Retry-After 或指数退避等待 |
| **TOC 同步**（`sync_toc.py`） | |
| `YUQUE_TOC_DELAY` | 每个 repo 请求后的间隔秒数，默认 0.3 |
| `YUQUE_TOC_MAX_RETRIES` | 单次 TOC 请求 429/5xx/网络错误时最大重试次数，默认 3 |
| `OUTPUT_DIR` | 输出目录（Git 仓库根），与 CLI `--output-dir` 二选一 |
| `PUSH_DECISION_MODE` | `llm` 或 `openclaw`：推送由本机 LLM 判定，或由 OpenClaw 回调判定 |
| **LLM 模式** | |
| `OPENAI_API_KEY` | LLM 模式必填（使用自定义认证头时可与下面二选一） |
| `OPENAI_BASE_URL` | 可选，默认 `https://api.openai.com/v1`，可改为任意 OpenAI 兼容或自建 API 基地址 |
| `OPENAI_CHAT_ENDPOINT` | 可选，默认 `chat/completions`，与 BASE_URL 拼接成完整请求 URL |
| `OPENAI_AUTH_HEADER_NAME` | 可选，自定义认证头名（如 `X-API-Key`），需与 `OPENAI_AUTH_HEADER_VALUE` 同时设置 |
| `OPENAI_AUTH_HEADER_VALUE` | 可选，自定义认证头取值；未设置时默认使用 `Authorization: Bearer OPENAI_API_KEY` |
| `OPENAI_MODEL` | 可选，默认 `gpt-4o-mini` |
| `ENABLE_UPDATE_SUMMARY` | 可选，`true`/`false`，是否让 LLM 生成变更摘要，默认 true |
| `NOTIFY_URL` | 判定推送后 POST 的 URL，body 含 `yuque_id`、`title`、`commit`、`update_summary` 等 |
| **邮件推送** | 全部设置后，判定推送时同时发邮件（与 NOTIFY_URL 可并存） |
| `SMTP_HOST` | SMTP 服务器地址 |
| `SMTP_PORT` | 可选，默认 587 |
| `SMTP_USER` / `SMTP_PASSWORD` | 可选，认证用（也可用 `SMTP_PASS`） |
| `SMTP_USE_TLS` | 可选，默认 `true` |
| `EMAIL_FROM` | 发件人地址 |
| `EMAIL_TO` | 收件人，多个用英文逗号分隔 |
| `GIT_PUSH_ON_PUSH` | 可选，`true` 时在判定推送后执行 `git push`，默认 false |
| `DIFF_MAX_CHARS` | 可选，给 LLM 的 diff 最大字符数，默认 6000 |
| `LLM_TIMEOUT` | 可选，LLM 请求超时秒数，默认 25 |
| `ENABLE_BODY_ONLY_DIFF` | 可选，`true` 时只对正文做 diff（去掉 frontmatter/表格），省 token、降噪，默认 true |
| **OpenClaw 模式** | |
| `OPENCLAW_CALLBACK_URL` | 待判定事件 POST 的 URL。**推荐**填 OpenClaw Gateway 的 Hooks 入口：`http(s)://<gateway>:<port>/hooks/agent`，则走官方 Hooks 协议（message + Bearer） |
| `OPENCLAW_HOOKS_TOKEN` | 当 URL 为 `/hooks/agent` 时必填，与 `~/.openclaw/openclaw.json` 的 `hooks.token` 一致，用于 `Authorization: Bearer` |
| `YUQUE2GIT_PUBLIC_URL` | 可选。yuque2git 服务对外可访问的 base URL（如 `http://host:8765`），写入 prompt 供 OpenClaw Agent 回调 `POST /mark-pushed`；未设则 prompt 中为占位说明 |
| `YUQUE2GIT_DELIVER_CHANNEL` | 可选。与 `YUQUE2GIT_DELIVER_TO` 同时设置时，请求 body 使用 `deliver: true` 并带 channel/to；channel 填 OpenClaw 中通道名（如 `qq`） |
| `YUQUE2GIT_DELIVER_TO` | 可选。投递目标，支持**多选**：逗号分隔多个 ID（如 `1179350197,群ID2`），同一 channel 下会收到同一条回复 |
| `YUQUE2GIT_DELIVER_TARGETS` | 可选。多目标 JSON 数组，覆盖 CHANNEL+TO。例：`[{"channel":"qq","to":"1179350197"},{"channel":"qq","to":"456"}]`。请求中会带 `deliver_to` 数组；若 Gateway 支持多目标投递，会向每项各发一条 |
| `YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE` | 可选。自定义发给 Agent 的整段 message；占位符：`{title}`、`{repo_name}`、`{repo_slug}`、`{doc_slug}`、`{diff}`、`{yuque_id}`、`{commit}`、`{callback_instruction}`、`{author}`、`{doc_url}`。若决定不推送，Agent 回复写 `[不发]` 则 QQ 通道不会发送该条 |
| `YUQUE_NAMESPACE` | 可选。语雀命名空间（团队/用户 login），用于生成原文地址；未设时从文档详情的 book.user.login 取 |
| `WEBHOOK_SECRET` | 可选，校验语雀 Webhook 签名 |

## 测试与部署（主仓库唯一）

- **测试时仅使用本主仓库**：Webhook 服务与全量同步、TOC 同步均应从**本仓库**（即本 Skill 所在目录，如 `/home/admin/yuque2git`）启动，不要从其它副本（如复制到 OpenClaw workspace 的 yuque2git）运行。否则可能出现路径规则不一致（例如仍写 slug 路径）、行为与文档不符。
- 语雀回调的 Webhook URL 指向的应是基于本主仓库启动的服务。

## 使用方式

1. **启动 Webhook 服务**  
   `python scripts/webhook_server.py --output-dir /path/to/repo [--port 8765] [--bind 0.0.0.0]`  
   确保服务可被语雀访问（内网 + 反向代理，或 ngrok/tailscale 等）。

2. **在语雀配置 Webhook**  
   知识库 → 设置 → Webhook，URL 填 `http(s)://your-host:port/webhook`。

3. **全量同步（可选）**  
   首次或需要重建时：`python scripts/sync_to_files.py --output-dir /path/to/repo`。可选 `--mark-all-pushed` 将当前 commit 视为已推送。

4. **TOC 同步**  
   语雀 **TOC 变更不触发 webhook**，需定期或按需执行：  
   `python scripts/sync_toc.py --output-dir /path/to/repo`  
   建议用 cron 每 5–15 分钟跑一次，或在全量同步时已写入各 repo 的 `.toc.json`。

5. **排错**  
   - Token 无效：检查 `YUQUE_TOKEN` 与语雀权限。  
   - 429：限流；脚本内已做并发与重试，可降低并发或稍后重试。  
   - 写文件权限：确保 `OUTPUT_DIR` 可写。  
   - 语雀回调超时：Webhook 内尽量快速返回 200；若 LLM 模式同步调 LLM 较慢，可考虑异步队列（见计划文档）。

## 只读约定（必读）

**本 Skill 同步出的知识库目录为只读。** OpenClaw/Agent **不得私自修改**其中文档；仅可读取、引用、检索。任何本地编辑都可能被下一次 Webhook 或全量同步覆盖。若需修改文档，请在语雀上编辑，由 Webhook 或同步脚本更新到本地。

## 删除与 Git

- 文档删除会立即写入 Git（commit 删除操作），历史可追溯。
- TOC 信息存于各 repo 的 `.toc.json`，需靠 TOC 同步脚本更新。

## 边界情况：父文档/目录移动

- 当**父文档在语雀 TOC 中被移动**（整棵子树挪到另一节点下）时，若语雀只下发父文档的 webhook，本服务只会更新父文档到新路径；**子文档不会收到事件**，会暂时仍留在旧路径，与语雀结构不一致。
- 子文档路径会在以下任一情况后恢复一致：语雀对子文档也触发了事件、或执行一次**全量同步**（`sync_to_files.py`）。建议在重要目录移动后跑一次全量同步。
- 全量同步会按当前 TOC 把父与子都写到新路径，并**根据 .yuque-id-to-path.json 删除已移动文档的旧路径文件**，避免孤儿文件。

## 智能推送与两种模式

- **Diff 基准**：以「最后推送时的文档状态」与当前状态做 diff，由 AI 或 OpenClaw 判定是否推送。
- **LLM 模式**（`PUSH_DECISION_MODE=llm`）：服务内调 LLM 得 YES/NO 与可选更新总结；需配置 `OPENAI_API_KEY` 等。无实质变更（diff 为「无文本变更」或「文档移动内容无变更」）时**不调 LLM**，直接不推送以省 token。默认只对**正文**做 diff（`ENABLE_BODY_ONLY_DIFF=true`），若推送则调用 `NOTIFY_URL` 并更新 last-push。
- **OpenClaw 模式**（`PUSH_DECISION_MODE=openclaw`）：服务把待判定事件 POST 到 `OPENCLAW_CALLBACK_URL`，由 OpenClaw 判定是否推送；完成后由 Agent 回调本服务 `POST /mark-pushed`（body 含 `yuque_id` 与 `commit`）更新 last-push。
  - **接入 OpenClaw Gateway Hooks**（推荐）：将 `OPENCLAW_CALLBACK_URL` 设为 `http(s)://<gateway>:<port>/hooks/agent`，并配置 `OPENCLAW_HOOKS_TOKEN`（与 openclaw 的 `hooks.token` 一致）、可选 `YUQUE2GIT_PUBLIC_URL`。若希望 Agent 的回复投递到 QQ：同时设置 `YUQUE2GIT_DELIVER_CHANNEL=qq` 与 `YUQUE2GIT_DELIVER_TO=<群ID或用户ID>`；默认 prompt 会约定「若决定不推送，回复只写 `[不发]`」，QQ 通道不会发送该条。自定义 prompt 可用 `YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE`，占位符见上表。若未配置 deliver channel/to，可在 OpenClaw Agent 的 system prompt 或 AGENTS 中约定：决定推送后由 Agent 用 qqbot 等自行向指定会话发一条通知。在 OpenClaw 侧：于 `~/.openclaw/openclaw.json` 的 `hooks` 下启用对外 Hooks（`hooks.enabled: true`、`hooks.token: "<共享密钥>"`），并确保 main（或目标）Agent 能访问 yuque2git（如允许 `exec` 执行 `curl` 调用 `YUQUE2GIT_PUBLIC_URL/mark-pushed`），网络允许 Gateway 访问 yuque2git 服务。

## 元数据

每篇文档为单文件：**YAML frontmatter**（机读）+ **文档开头 Markdown 表格**（作者、创建时间、更新时间等，人读）+ 正文。frontmatter 含 `yuque_id`、`title`、`slug`、`repo_id`、`repo_slug`、`created_at`、`updated_at`、`user_id`、`last_editor_id`、`author_name` 等，与语雀 get_doc_detail 一致。

输出目录根下另有 **`.yuque-id-to-path.json`**：记录 `yuque_id → 相对路径`，用于文档移动后仍能按「旧路径 vs 新路径」正确算 diff，与 `.yuque-last-push.json` 一起由服务与全量同步维护。
