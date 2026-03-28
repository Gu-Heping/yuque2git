# yuque2git 项目指南

## 项目概述

将语雀文档实时同步到本地 Git 仓库的工具。通过 Webhook 接收语雀事件，将文档写成 Markdown（带 YAML frontmatter）并自动 commit。支持智能推送判定：由 AI 决定是否推送变更通知给订阅者。

## 核心功能

1. **Webhook 实时同步** - 接收语雀 publish/update/delete 事件，实时写入 Markdown
2. **全量同步** - 拉取用户所有知识库，重建本地镜像
3. **TOC 同步** - 同步目录结构（TOC 变更不触发 webhook）
4. **智能推送** - LLM/OpenClaw 根据 diff 判定是否推送通知
5. **多渠道投递** - 支持 QQ（Napcat OneBot）和 Discord Bot 直连

## 技术栈

- Python 3.9+
- FastAPI + uvicorn（Webhook 服务）
- httpx（异步 HTTP 客户端）
- PyYAML（frontmatter 处理）

## 项目结构

```
yuque2git/
├── README.md           # 使用文档
├── SKILL.md            # OpenClaw Skill 说明
├── CLAUDE.md           # 本文件
├── docs/
│   └── openclaw-summary-prompt.md  # 推送降噪说明
└── scripts/
    ├── webhook_server.py      # 主服务：Webhook + 推送判定 + 回调
    ├── sync_to_files.py       # 全量同步
    ├── sync_toc.py            # TOC 同步
    ├── requirements.txt       # Python 依赖
    └── tests/
        └── test_webhook_server.py  # 单元测试
```

## 关键入口

| 脚本 | 用途 | 命令示例 |
|------|------|----------|
| `webhook_server.py` | Webhook 服务 | `python scripts/webhook_server.py --output-dir /path --port 8765` |
| `sync_to_files.py` | 全量同步 | `python scripts/sync_to_files.py --output-dir /path` |
| `sync_toc.py` | TOC 同步 | `python scripts/sync_toc.py --output-dir /path` |

## 核心环境变量

| 变量 | 说明 | 必填 |
|------|------|------|
| `YUQUE_TOKEN` | 语雀 API Token | ✅ |
| `OUTPUT_DIR` | 输出目录（Git 仓库根） | ✅ |
| `PUSH_DECISION_MODE` | `llm` 或 `openclaw` | 推送判定模式 |
| `OPENCLAW_CALLBACK_URL` | OpenClaw Gateway Hooks URL | OpenClaw 模式需填 |
| `OPENCLAW_HOOKS_TOKEN` | 与 Gateway hooks.token 一致 | OpenClaw 模式需填 |
| `YUQUE2GIT_PUBLIC_URL` | yuque2git 对外地址 | Agent 回调用 |
| `YUQUE2GIT_DELIVER_CHANNEL` | `qq` 或 `discord` | 投递通道 |
| `YUQUE2GIT_DELIVER_TO` | 目标（`g:群号`/`p:QQ号`/`channel:id`/`user:id`） | 投递目标 |
| `YUQUE2GIT_DIRECT_SEND_URL` | Napcat HTTP 地址 | QQ 直连 |
| `YUQUE2GIT_DISCORD_BOT_TOKEN` | Discord Bot Token | Discord 直连 |

## 输出格式

每篇文档：**YAML frontmatter** + **元数据表格** + **正文**

```
---
id: 123
title: 文档标题
slug: abc123
created_at: 2024-01-01 10:00:00
updated_at: 2024-01-02 15:30:00
author: 创建者姓名
book_name: 知识库名称
---

| 作者 | 创建时间 | 更新时间 |
|------|----------|----------|
| 创建者姓名 | 2024-01-01 10:00:00 | 2024-01-02 15:30:00 |

正文内容...
```

## 辅助文件

| 文件 | 位置 | 作用 |
|------|------|------|
| `.yuque-id-to-path.json` | OUTPUT_DIR 根 | id → 相对路径映射，用于文档移动后正确 diff |
| `.yuque-last-push.json` | OUTPUT_DIR 根 | 上次推送的 commit hash |
| `.yuque-members.json` | OUTPUT_DIR 根 | 团队成员姓名缓存 |
| `.yuque-pending-pushes.jsonl` | OUTPUT_DIR 根 | 回调失败队列 |
| `.toc.json` | 各知识库目录 | TOC 结构缓存 |

## 特殊文档类型

- **lakesheet**：语雀表格，渲染为 TSV 代码块
- **laketable**：多维表格，渲染为 TSV（支持 mention、select、date 等类型）

## 推送判定流程

### LLM 模式
1. 获取当前文档与上次推送时的 diff
2. 无实质变更 → 不调 LLM，不推送
3. 调 LLM → 返回 `should_push` + `update_summary`
4. `should_push=true` → 调用 NOTIFY_URL，更新 last-push

### OpenClaw 模式
1. POST 到 `OPENCLAW_CALLBACK_URL`（含 diff + 回调契约）
2. Agent 回调 `/mark-pushed`
3. yuque2git 统一生成摘要文案并投递

## 回调契约（OpenClaw）

Agent 回调 `/mark-pushed` 须携带：

```json
{
  "yuque_id": 261997991,  // 整数 id，非 slug
  "commit": "e80935e",
  "should_push": true,
  "summary": {
    "title": "文档标题",
    "repo_name": "知识库",
    "author": "作者",
    "doc_url": "https://nova.yuque.com/namespace/repo/doc",
    "highlights": ["要点1", "要点2"]  // 1~3 条
  }
}
```

## 推送降噪

服务端预筛（可选）：
- `YUQUE2GIT_OPENCLAW_MIN_DIFF_CHARS`：diff 过小不调用
- `YUQUE2GIT_OPENCLAW_COOLDOWN_SECONDS`：冷却期内不调用
- `YUQUE2GIT_OPENCLAW_COOLDOWN_BYPASS_CHARS`：大 diff 可绕过冷却

提示词降噪原则（默认）：
- 错别字/标点微调 → `should_push=false`
- 新增整节/重要变更 → `should_push=true`
- 无把握时默认 `false`

## 测试

```bash
python -m pytest scripts/tests/test_webhook_server.py -v
```

覆盖：slug 安全处理、TOC 路径解析、Markdown 构建、diff 计算、Webhook 解析、OpenClaw 回调格式等。

## 只读约定

同步出的知识库目录为**只读**。不得私自修改其中文档，仅可读取、引用。本地编辑会被下次同步覆盖。

## Git 工作流

每次 webhook/同步都会：
1. 写入/删除文件
2. `git add -A`
3. `git commit -m "..."`

可选 `GIT_PUSH_ON_PUSH=true` 在判定推送后执行 `git push`。

## 常见错误

| 错误 | 原因 | 解决 |
|------|------|------|
| `422 int_parsing` | `yuque_id` 传了字符串 slug | 传整数 id |
| `400 summary missing` | `should_push=true` 但 summary 不完整 | 补全所有字段 |
| 429 | 语雀 API 限流 | 降低并发/稍后重试 |