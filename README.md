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
   - 目录结构：`{OUTPUT_DIR}/{知识库slug}/{父路径}/{文档slug}.md`，根文档为 `{OUTPUT_DIR}/{知识库slug}/{文档slug}.md`。根下另有 `.yuque-id-to-path.json`（yuque_id→路径索引）、`.yuque-last-push.json`（上次推送 commit），由服务与全量同步维护。
3. 配置环境变量：`YUQUE_TOKEN`（必填）。
4. 启动 Webhook 服务：`.venv/bin/python scripts/webhook_server.py --output-dir /path/to/repo --port 8765`（若已设 `OUTPUT_DIR` 可省略 `--output-dir`）
5. 在语雀知识库后台配置 Webhook URL 指向该服务（如 `http://your-host:8765/webhook`）。
6. 可选：运行 `scripts/sync_to_files.py` 做全量同步；运行 `scripts/sync_toc.py` 定期同步 TOC（语雀 TOC 变更不触发 webhook）。二者若不传 `--output-dir` 会使用环境变量 `OUTPUT_DIR`。

**智能 diff**：推送判定以前一次推送为基准做 diff。输出目录内的 `.yuque-id-to-path.json` 记录每篇文档的路径；文档在语雀中移动父节点导致路径变化时，仍能按「旧路径 vs 新路径」正确算 diff。详见 [SKILL.md](SKILL.md) 中的配置项、推送模式（LLM / OpenClaw）与排错说明。

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
