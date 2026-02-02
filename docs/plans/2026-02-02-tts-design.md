# Jarvis TTS 语音回复设计

## 目标
- 在合适场景下为 Jarvis 增加语音回复能力。
- 采用 Codex 显式标记触发，不做硬编码长度规则。
- 故事类回复无论长短都应发送语音。

## 触发策略（Codex 标记）
- 需要语音播报的内容使用 `<tts>...</tts>` 包裹。
- 系统提取 `<tts>` 内容生成语音，并从文本回复中移除该部分。
- 故事类回复需将完整故事放入 `<tts>...</tts>`；其他内容保持保守，仅在适合语音时包裹。

## 组件与数据流
1. `PromptBuilder` 在 prompt 头部注入 TTS 规则提示。
2. `CodexManager` 提取 `<tts>` 内容并从响应文本中剥离，得到 `tts_text`。
3. `MessagePipeline` / `TaskPipeline` 在发送文本时传入 `tts_text`。
4. `Messenger` 在文本发送完成后，若存在 `tts_text` 则调用 `TTSService` 生成语音文件，并发送为 Telegram 音频。
5. `TelegramBot` 使用既有媒体发送逻辑发送音频文件。

## TTS 生成
- 使用 `edge-tts` 生成语音，默认输出为 MP3。
- 若本地存在 ffmpeg/sox，则转码为 ogg/opus 以符合 Telegram 推荐格式。
- 输出文件保存在 Telegram media 目录，文件名包含时间戳与内容 hash。
- 语音以音频（audio/voice）方式发送，文本仍优先发送。

## 错误处理与回退
- TTS 失败、超时或依赖缺失时，记录日志并仅发送文本。
- 不影响主对话流程。

## 配置
- 新增 `tts` 配置块（enabled、voice、rate、pitch、timeout、重试等）。
- `tts` 未配置时使用默认值，保持开箱即用。

## 测试建议
- 单次对话输出包含 `<tts>...</tts>`，验证文本与音频发送。
- 无 `<tts>` 时仅发送文本。
