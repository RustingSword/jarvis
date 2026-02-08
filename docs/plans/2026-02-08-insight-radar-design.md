# Insight Radar 设计（2026-02-08）

## 目标
- 基于 Twitter/RSS/HN 的最新内容自动发现“新颖性/影响/争议”较高的主题。
- 自动扩展调研相关来源，生成结构化深度解读（Markdown + PDF）。
- 记录已处理主题与证据增量，避免重复推送。
- 无命中时静默（返回 `HEARTBEAT_OK`）。

## 约束与范围
- 不修改 `config.yaml` 或 `.env`。
- 不引入新的外部依赖；复用现有库（`aiohttp`、`trafilatura`、`pandoc/lualatex`）。
- 触发方式优先复用 heartbeat（无需新增触发器）。

## 架构
1) 采集层：读取最新 Twitter/HN raw JSON 与 RSS digest `.md`。
2) 候选层：LLM 对条目进行“新颖性/影响/争议/相关度”评估，选出 1–3 个候选主题。
3) 扩展调研层：对每个主题执行轻量搜索，补充 3–5 个来源并抓取正文。
4) 生成层：按固定结构输出深度解读（概述/证据/影响/风险/指标）。
5) 状态层：记录主题指纹与来源集合，支持证据增量判断。

## 触发与执行
- 在 `HEARTBEAT.md` 中加入 `@run_always` 指令与 radar 任务说明。
- Heartbeat 每次 cron 触发时执行 radar；无命中则返回 `HEARTBEAT_OK` 静默退出。

## 去重与证据增量
- 主题 key：规范化标题（小写、去标点）。
- 状态字段：`sources/first_seen/last_seen/evidence_count`。
- 若新来源集合对比历史有增量，则允许再次输出（视为“证据更新”）。

## 输出格式
- Markdown + PDF（固定生成）。
- 报告结构：概述 → 证据与来源 → 影响/机会 → 风险与反对观点 → 可观察指标 → 参考来源。
- 输出目录：`reports/radar/`，并保留 `.md` 便于检索。

## 错误处理
- 缺少 API key：输出错误提示，不静默。
- 搜索或抓取失败：降级为“轻量解读”，并标注证据受限。
- PDF 生成失败：保留 Markdown，提示失败原因。

## 测试与验收
- 能在本地运行 `scripts/radar_run.py --mode heartbeat` 成功生成 PDF。
- Heartbeat 无命中时返回 `HEARTBEAT_OK`。
- 证据增量时重复主题可再次输出。
