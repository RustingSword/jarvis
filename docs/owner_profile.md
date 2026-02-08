# 主人画像（稳定）

Last updated: 2026-02-05
Status: active

## 身份与角色偏好
- statement: 期望助理更主动，能开发工具、分析对话历史，并沉淀为长期文档与触发器候选。
  category: workflow
  confidence: high
  source: 2026-02-05 命令日志（排除定时触发任务）+ 明确指令
  status: active

## 沟通风格
- statement: 默认使用中文思考与回复。
  category: format
  confidence: high
  source: 2026-02-05 命令日志（排除定时触发任务）+ AGENTS 指令
  status: active
- statement: 偏好简洁直接的回复，但在需要时要求深入细化。
  category: format
  confidence: medium
  source: 2026-02-05 命令日志（排除定时触发任务）
  status: active
- statement: 喜欢选项式推进（A/B/C）并快速确认。
  category: workflow
  confidence: medium
  source: 2026-02-05 命令日志（排除定时触发任务）
  status: active
- statement: 当明确要求“字以内”时需给短答。
  category: format
  confidence: high
  source: 2026-02-05 命令日志（频繁“字以内”）
  status: active

## 交付偏好
- statement: PDF 为主要交付物，优先 LaTeX/Pandoc/LuaLaTeX 方案。
  category: preference
  confidence: high
  source: 2026-02-05 命令日志（排除定时触发任务）
  status: active
- statement: 对排版质量敏感（CJK 字体、换行、列表、代码高亮、图片比例/清晰度）。
  category: preference
  confidence: high
  source: 2026-02-05 命令日志（排除定时触发任务）
  status: active

## 决策与风险边界
- statement: 重操作放到 nightly build 执行。
  category: risk
  confidence: high
  source: 2026-02-05 明确指令
  status: active
- statement: 修改配置/密钥等高风险操作需先确认，优先可回滚与最小改动。
  category: boundary
  confidence: high
  source: 2026-02-05 命令日志（排除定时触发任务）+ AGENTS 指令
  status: active

## 协作节奏
- statement: 偏好快速迭代与即时验证（改-跑-看-再改）。
  category: workflow
  confidence: high
  source: 2026-02-05 命令日志（排除定时触发任务）
  status: active
- statement: 需要时会要求提交代码并重启服务。
  category: workflow
  confidence: medium
  source: 2026-02-05 命令日志（排除定时触发任务）
  status: active

## 常用任务与关注领域
- statement: 高关注：RSS/HN/Twitter 内容抓取与汇总、报告生成、PDF 输出。
  category: workflow
  confidence: high
  source: 2026-02-05 命令日志（排除定时触发任务）
  status: active
- statement: 关注 agent 能力建设（memory/skills/heartbeat/trigger/TTS/多模态）。
  category: workflow
  confidence: high
  source: 2026-02-05 命令日志（排除定时触发任务）
  status: active

## 触发器候选池（未启用）
- statement: Nightly 汇总画像候选变更与潜在触发器清单（仅报告）。
  category: trigger_candidate
  confidence: medium
  source: 2026-02-05 命令日志（排除定时触发任务）+ 实施指令
  status: pending-confirm

## 禁区/敏感
- statement: 未经确认不改配置/密钥/破坏性操作。
  category: boundary
  confidence: high
  source: 2026-02-05 命令日志（排除定时触发任务）+ AGENTS 指令
  status: active

## Rule format
- statement:
- category:
- confidence: low | medium | high
- source: YYYY-MM-DD + evidence line
- status: active | pending-confirm | deprecated
