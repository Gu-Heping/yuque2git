# OpenClaw 推送判定与摘要（yuque2git 发出）

## 推送门槛（降噪）

服务端在发给 Agent 的 **reply_contract** 与默认 message 中约定：

- **默认保守**：无把握时一律 `should_push=false`。
- **应 `should_push=false`**：错别字/标点微调、纯排版或仅空格换行、单句措辞润色、无关紧要的链接或元数据微调、极小片段且无信息增量。
- **可 `should_push=true`**：新增或重写整节、多段逻辑/结构变化、重要结论/决策/数据/API 约定变更、明显阶段性成果的篇幅与语义变化；diff 极少但信息极重要也可为 true，且 `highlights` 须写明原因。

默认 message 首段另有一句总原则（常量 `OPENCLAW_PUSH_POLICY_SHORT`）；自定义模板可使用占位符 **`{push_policy}`** 插入同一段。

## 默认完整 message 结构（未设 `YUQUE2GIT_OPENCLAW_MESSAGE_TEMPLATE`）

```
【yuque2git 推送判定】

{OPENCLAW_PUSH_POLICY_SHORT}

文档标题：…
…
请根据以下 diff 是否推送到远程。{callback_instruction}
{reply_contract}

---

Diff:
{diff}
```

`reply_contract` 内含上述门槛 + JSON 回调契约（见仓库 `scripts/webhook_server.py` 中 `_build_openclaw_reply_contract`）。

## 服务端预筛（可选环境变量）

| 变量 | 说明 |
|------|------|
| `YUQUE2GIT_OPENCLAW_MIN_DIFF_CHARS` | 大于 0 时：diff 字符数低于该值则**不调用** OpenClaw（打 info 日志）。`0` 表示关闭。 |
| `YUQUE2GIT_OPENCLAW_COOLDOWN_SECONDS` | 大于 0 时：某文档成功投递摘要后，同一 `yuque_id` 在冷却期内不再调用 OpenClaw。时间戳写在 `OUTPUT_DIR/.yuque-openclaw-push-cooldown.json`。 |
| `YUQUE2GIT_OPENCLAW_COOLDOWN_BYPASS_CHARS` | 冷却期内若 diff 字符数 ≥ 该值仍调用 OpenClaw（`0` 表示不启用绕过）。 |

建议：先依赖提示词降噪；仍偏频时再开 `MIN_DIFF` 与 `COOLDOWN`。

## 代码位置

- 门槛文案：`OPENCLAW_PUSH_GATE_RULES`、`OPENCLAW_PUSH_POLICY_SHORT`
- 契约：`_build_openclaw_reply_contract`
- 预筛：`_openclaw_precall_skip_reason`；冷却写入：`_record_openclaw_push_cooldown_now`（在 `/mark-pushed` 且摘要投递成功后）
