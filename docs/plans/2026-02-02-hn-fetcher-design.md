# HN 内容抓取脚本设计（hn_fetch.py）

## 目标
提供一个“只负责抓取与落盘”的 Hacker News 首页内容抓取脚本，输出标准 JSON，后续分析与报告由 Codex 完成。默认抓取 20 条故事、每条 30 条顶层评论，并尽量获取文章正文内容。

## 架构与数据流
- 数据源：HN Firebase API（`/v0/topstories` + `/v0/item/<id>`）
- 抓取流程：
  1) 获取 topstories 列表
  2) 顺序抓取前 N 条 story
  3) 对每条 story 抓取最多 M 条顶层评论（过滤 dead/deleted）
  4) 若 story 有 url，使用 `r.jina.ai` 抓取正文文本
- 输出：`reports/hn/hn_raw_YYYY-MM-DD_HHMM.json`

## 输出 JSON 结构
- 顶层字段：`generated_at`（本地时间 ISO）、`count`、`source`、`items[]`
- `items[]` 字段：
  - `id/title/url/by/score/time/descendants`
  - `story_text`（Ask HN 等自带文本，HTML 清洗后）
  - `article_text`（正文抓取文本，失败为空）
  - `comments[]`：`id/by/time/text`（HTML 清洗后）

## 错误处理与稳定性
- 统一超时：story/评论请求默认 10s，正文默认 8s
- 单条失败跳过或留空，不中断整体输出
- 文本清洗仅移除 HTML 标签 + 压缩空白

## 可用性与测试
- CLI 参数：`--limit/--comments/--out-dir/--timeout/--article-timeout`
- 最小验证：`./scripts/hn_fetch.py --limit 3 --comments 2`
- 输出文件路径与条目数会打印到 stdout

## 取舍
- 采用标准库顺序抓取：依赖少、行为可控
- 不做自动重试/并发：保持简单，可后续扩展
