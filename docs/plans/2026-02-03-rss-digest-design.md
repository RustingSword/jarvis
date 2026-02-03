# RSS 订阅与晨间摘要设计

日期：2026-02-03

## 目标
- 每天早上 08:00（本地时区）检查一份预处理的 RSS/Atom 源清单。
- 若发现新内容，生成中文摘要/翻译并推送到 Telegram。
- 不中断现有 Codex 任务链路，RSS 作为内部模块独立运行。

## 方案概述
- **清单预处理**：首次从 gist 提取 feed URL，写入本地 `data/rss_feeds.txt`，日常不再访问 gist。
- **定时触发**：新增 scheduler job，`action: rss` 时直接调用 RSS 服务，不走 Codex。
- **抓取解析**：aiohttp 并发拉取，feedparser 解析 RSS/Atom；失败源跳过。
- **去重状态**：`~/.jarvis/rss_state.json` 保存每源最近已发送 ID 与 last_seen，原子写入。
- **摘要翻译**：优先调用 OpenAI 生成三段式摘要（要点/细节/影响）；无 key 或失败时降级为简易三段摘要。
- **推送格式**：按源分组输出「标题 + 中文摘要 + 原文链接」。

## 关键配置（新增）
- `rss.enabled` 是否启用
- `rss.feeds_path` 本地 feed 清单路径
- `rss.state_path` 状态文件
- `rss.concurrency` 抓取并发数
- `rss.timeout_seconds` 单源超时
- `rss.max_items_per_feed` 每源最多条目
- `rss.max_total_items` 当次最多条目
- `rss.summary_max_chars` 摘要长度
- `rss.translate` 是否翻译
- `rss.openai_model` 摘要/翻译模型
- `rss.fulltext_enabled` 是否抓取正文
- `rss.fulltext_concurrency` 正文抓取并发
- `rss.fulltext_timeout_seconds` 正文超时
- `rss.fulltext_min_chars` 正文最小长度（过短则回退到 feed 摘要）
- `rss.fulltext_max_chars` 正文最大长度（截断）
- `rss.pdf_enabled` 是否生成 PDF
- `rss.pdf_output_dir` PDF 输出目录
- `rss.pdf_backend/pdf_template` 使用 pandoc 模板渲染 PDF（可回退 reportlab）

## 容错策略
- 单源失败不影响整体。
- 解析失败降级为仅标题+链接。
- 翻译失败降级为原文摘要。
- 无新内容默认不推送。

## 交付物
- `jarvis/rss/*` 模块
- `scripts/rss_update_from_gist.py` 预处理清单脚本
- `scripts/rss_run_once.py` 手动运行脚本
- `data/rss_feeds.txt` 初始清单
- `config.sample.yaml` 新增 RSS 配置示例
- Scheduler 增加 `rss_morning_digest`（需用户确认后写入 config.yaml）
