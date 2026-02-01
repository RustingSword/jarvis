# Jarvis 语音自动转写（Whisper）设计文档

## 概述

在现有 Telegram 消息处理链路上，新增自动语音转写能力：收到语音消息（voice）或音频文件（audio）时，自动调用 OpenAI 的语音转写服务，将转写文本合并进用户输入，再交给 Codex 继续处理。对用户不单独回复转写文本，保持对话体验一致。

## 目标

- 语音消息与音频文件自动转写
- 自动语言识别
- 转写文本直接进入对话，不单独发送给用户
- 转写失败时不打扰用户（仅日志记录）
- 保持改动最小，可开关、可回滚

## 非目标

- 不处理圆形视频（video note）
- 不做本地转码/降噪
- 不做分段流式转写
- 不做说话人分离（diarization）

## 用户决策摘要

- **触发方式**：自动转写
- **转写对象**：voice + audio
- **语言**：自动识别
- **输出**：仅进入对话，不单独回复
- **失败处理**：静默（仅日志）

## 架构与数据流

新增“转写预处理”步骤，放在 PromptBuilder 之前：

1. TelegramBot 下载媒体文件至 `media_dir`
2. MessageBundler 汇总消息（文本+附件）
3. MessagePipeline 在构建 prompt 前调用 TranscriptionService
4. TranscriptionService 识别 voice/audio，转写并返回文本
5. 转写文本合并到原始文本中；转写过的音频附件从附件列表剔除
6. PromptBuilder 继续构建 prompt -> Codex -> 回复

## 组件设计

### TranscriptionService

- 位置：`jarvis/audio/transcriber.py`
- 职责：将本地音频文件转写为文本
- 依赖：OpenAI Audio Transcriptions API（HTTP multipart）
- 产出：`text`（空字符串表示失败或无文本）

### 集成点

- `MessagePipeline.handle()`：在构建 prompt 前调用转写服务
- 仅处理附件中 `type in {voice, audio}` 的文件

## 配置设计

新增 `openai.audio` 配置段：

```yaml
openai:
  audio:
    enabled: true
    model: whisper-1
    response_format: json
    timeout_seconds: 30
    max_retries: 2
    retry_backoff_seconds: 0.5
```

环境变量：

```
OPENAI_API_KEY=...
```

优先级：`config.yaml` < 环境变量

## 错误处理与重试

- 缺少 `OPENAI_API_KEY` 或 `enabled=false`：跳过转写
- 网络/超时/服务端错误：按配置重试；最终失败仅写日志
- 失败不向用户发送提示，避免打断对话

## 日志与可观测性

- 记录：耗时、模型名、文件大小、成功/失败计数
- 不记录转写文本，避免敏感信息泄露

## 测试策略

- 单元测试：
  - TranscriptionService 成功/失败/超时
  - MessagePipeline 在“仅语音/语音+文本/失败”时文本合并与附件过滤
- 若缺少测试框架，可先提供最小本地自检脚本

## 部署与回滚

- 部署：配置 `OPENAI_API_KEY`，设置 `openai.audio.enabled=true`，重启服务
- 回滚：设置 `openai.audio.enabled=false` 或移除 API Key

## 风险与缓解

- 成本增长：可按模型与频率控制
- 识别错误：保留原始文本并允许后续手动纠正
- 噪声/长音频：限制文件大小与超时，必要时后续再做转码/切分
