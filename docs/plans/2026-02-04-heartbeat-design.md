# Jarvis Heartbeat（最简）设计

日期：2026-02-04

## 目标
- 默认静默运行，不发消息、不更新 session。
- 仅当 `HEARTBEAT.md` 内容发生变化且非空时，触发一次新的会话进行用户交互。
- 其他需求由 `HEARTBEAT.md` 直接表达，Heartbeat 机制只负责“发现变化并触发”。

## 方案概述
- 复用现有 Scheduler，新增 `action: heartbeat`。
- TriggerDispatcher 增加 heartbeat 分支，调用 `HeartbeatRunner`。
- `HeartbeatRunner` 只读取 `HEARTBEAT.md`（或 `heartbeat.md`），比较内容 hash；无变化则结束。
- 有变化且非空时，触发一次 `trigger.message`，让 Codex 创建新的会话处理文件内容。

## 默认输入
- 仅从 `HEARTBEAT.md`（优先）读取；若不存在则尝试 `heartbeat.md`。
- 不读取运行日志、不扫描其他文件。

## 判定逻辑
- 读取文件内容，去掉空白行后若为空，视为“无任务”。
- 将规范化内容计算 hash，与 `heartbeat_state.json` 中的 `last_hash` 比较。
- **未变化或为空**：仅记录日志并更新 `last_checked_at`。
- **变化且非空**：构造触发消息（内容为文件原文或加一行前缀说明），触发新会话。

## 会话与消息策略
- 使用 `source=trigger` 触发消息，沿用现有 Trigger → MessagePipeline 流程。
- 触发时创建新的 session，但不会设置为 active（不干扰现有会话）。
- 未触发时不调用 Codex，因此不会产生新的 session。

## 状态文件
- `~/.jarvis/heartbeat_state.json`
  - `last_hash`: 上次触发时的内容 hash
  - `last_checked_at`: 上次检查时间
  - `last_trigger_at`: 上次触发时间（可选）

## 配置（最简）
- `triggers.scheduler` 增加 job：
  - `name: heartbeat`
  - `cron: "*/30 * * * *"`（示例，可按需调整）
  - `action: heartbeat`
  - `chat_id: <目标 chat_id>`（仅在需要触发时使用）

## 容错策略
- `HEARTBEAT.md` 不存在：静默退出并记录 debug。
- 状态文件损坏：回退为“无状态”检查并重建。
- Runner 异常：仅记录日志，不触发消息。

## 测试建议
1. `HEARTBEAT.md` 为空 → 心跳不触发。
2. 写入一条指令 → 触发一次新会话。
3. 不改内容 → 不再触发。
4. 修改内容 → 再次触发。

## 交付物
- `jarvis/heartbeat/runner.py`（新增）
- `jarvis/handlers/trigger_dispatcher.py`（新增 heartbeat 分支）
- `config.sample.yaml`（示例 job）
- `docs/plans/2026-02-04-heartbeat-design.md`（本文档）
