# yuque2git

语雀文档同步到本地 Git 仓库：Webhook 实时写 Markdown 并 commit，由 AI 判定是否智能推送并通知订阅者。

- **仓库**：<https://github.com/Gu-Heping/yuque2git>
- **形态**：既可作为 OpenClaw 的 Skill 使用，也可作为独立任务单独运行（同一套脚本与配置）。

## 快速开始

1. 配置环境变量：`YUQUE_TOKEN`（必填）、`OUTPUT_DIR`（输出目录，需为或将成为 Git 仓库）。
2. 启动 Webhook 服务：`python scripts/webhook_server.py --output-dir /path/to/repo --port 8765`
3. 在语雀知识库后台配置 Webhook URL 指向该服务（如 `http://your-host:8765/webhook`）。
4. 可选：运行 `scripts/sync_to_files.py` 做全量同步；运行 `scripts/sync_toc.py` 定期同步 TOC（语雀 TOC 变更不触发 webhook）。

详见 [SKILL.md](SKILL.md) 中的配置项、推送模式（LLM / OpenClaw）与排错说明。

## 目录结构

```
yuque2git/
├── README.md
├── SKILL.md          # OpenClaw Skill 说明
└── scripts/
    ├── requirements.txt
    ├── webhook_server.py   # Webhook 服务（接收语雀推送、写文件、commit、智能推送）
    ├── sync_to_files.py    # 全量同步
    └── sync_toc.py         # 仅同步 TOC
```

## License

与上游 yuque-sync-platform 的复用思路一致；本仓库代码可独立使用。
