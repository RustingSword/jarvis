# Jarvis 清理重启脚本设计

日期：2026-02-02

## 背景
当前重启可能出现旧进程未完全退出，导致多实例并存，Telegram bot 轮询冲突、启动通知缺失等问题。需要一个类似 `run.sh` 的脚本，专门用于“清理旧进程 + 重启”，并支持延迟执行。

## 目标
- 清理残留 Jarvis 进程，确保单实例运行。
- 支持延迟执行，适配后台重启模式。
- 低侵入：不改现有 `run.sh`，新增独立脚本。

## 方案
新增 `scripts/restart_clean.sh`：
- 优雅停机：先调用 `./scripts/run.sh stop`（仅处理 `.jarvis.pid`）。
- 残留检测：`pgrep -f "python -m jarvis --config <CONFIG_PATH>"` 获取残留 PID。
- 强制清理：默认不强制；只有 `--force` 才会发送 TERM/KILL。
- 延迟执行：支持 `--delay <秒>`。
- 预演模式：`--dry-run` 仅输出动作。

## 参数/用法
- `--delay <秒>`：延迟执行清理与重启。
- `--force`：允许强制清理残留进程（TERM→KILL）。
- `--dry-run`：仅打印，不执行。

示例：
- 预演：`./scripts/restart_clean.sh --dry-run`
- 安全重启：`./scripts/restart_clean.sh --delay 2`
- 强制重启：`./scripts/restart_clean.sh --delay 2 --force`

## 错误处理
- 找不到 venv 或 `run.sh` 失败：直接退出并提示。
- 存在残留且未 `--force`：提示 PID 并退出。
- 清理/启动失败：提示日志路径并退出。

## 验证
- 运行后检查只有一个 Jarvis 进程。
- `.jarvis.pid` 与新进程 PID 一致。
- Telegram 启动通知恢复。
