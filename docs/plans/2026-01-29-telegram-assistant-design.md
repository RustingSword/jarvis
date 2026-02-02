# Jarvis - Telegram 个人助理 Agent 设计文档

## 概述

基于 Codex SDK 实现一个长期在线的个人助理 agent，通过 Telegram bot 进行消息双向传递，支持被动响应用户指令和主动根据信号发起消息。

## 需求总结

### 功能需求
- **综合助理功能**：信息查询、系统监控、记忆管理
- **混合触发方式**：时间触发、系统事件、外部 API
- **双向通信**：被动响应 + 主动推送
- **会话管理**：类似 clawdbot 的 session 管理（独立上下文、/reset、/compact）

### 技术需求
- **部署方式**：本地服务器/VPS，systemd 服务
- **数据持久化**：对话历史、监控配置、用户偏好
- **配置管理**：基础规则用配置文件，临时规则用命令

## 架构设计

### 整体架构：事件驱动架构

```
┌─────────────────────────────────────────────────────┐
│                  Jarvis Agent                        │
├─────────────────────────────────────────────────────┤
│                                                      │
│  ┌──────────────┐         ┌──────────────┐         │
│  │ Telegram Bot │◄───────►│  Event Bus   │         │
│  └──────────────┘         └──────┬───────┘         │
│                                   │                  │
│  ┌──────────────┐                │                  │
│  │Codex Manager │◄───────────────┤                  │
│  └──────────────┘                │                  │
│                                   │                  │
│  ┌──────────────┐                │                  │
│  │Trigger System│◄───────────────┤                  │
│  │ - Scheduler  │                │                  │
│  │ - Monitor    │                │                  │
│  │ - Webhook    │                │                  │
│  └──────────────┘                │                  │
│                                   │                  │
│  ┌──────────────┐                │                  │
│  │Storage Layer │◄───────────────┘                  │
│  │ - SQLite     │                                    │
│  │ - File Store │                                    │
│  └──────────────┘                                    │
└─────────────────────────────────────────────────────┘
```

### 技术栈
- Python 3.11+
- python-telegram-bot（异步版本）
- asyncio 事件循环
- SQLite3 + aiosqlite
- APScheduler（定时任务）
- Codex CLI（通过 subprocess 调用）

## 核心组件设计

### 1. Event Bus（事件总线）

**职责：**
- 管理事件订阅和发布
- 异步分发事件到所有订阅者
- 提供事件日志（用于调试）

**核心事件类型：**
- `telegram.message_received` - 用户发送消息
- `telegram.send_message` - 向用户发送消息
- `codex.session_start` - 开始 Codex 会话
- `codex.response_ready` - Codex 响应就绪
- `trigger.fired` - 触发器触发
- `storage.save` / `storage.load` - 数据存储事件

### 2. Telegram Bot Manager

**职责：**
- 管理 Telegram Bot 连接
- 接收用户消息并发布事件
- 监听发送消息事件并执行

**关键功能：**
- 命令处理（/start, /reset, /compact, /help 等）
- 消息路由（普通消息 vs 命令）
- 错误处理和重试机制

### 3. Codex Manager

**职责：**
- 封装 Codex CLI 调用
- 管理 session ID 到 Telegram chat 的映射
- 处理 session 重置和压缩

**实现方式：**
- 使用 asyncio.create_subprocess_exec 调用 codex CLI
- 每个 Telegram chat_id 对应一个 Codex session
- Session ID 从 Codex 输出中提取并持久化
- 支持 /reset 清空 session

**注意：Codex 没有官方 Python SDK，使用 CLI wrapper 方式**

### 4. Trigger System（触发器系统）

**组件结构：**
- **SchedulerTrigger**: 使用 APScheduler 管理定时任务
- **MonitorTrigger**: 监控系统指标（CPU、内存、磁盘、进程）
- **WebhookTrigger**: 接收外部 webhook 通知

**触发流程：**
1. 触发器检测到条件满足
2. 发布 `trigger.fired` 事件
3. 事件总线分发到处理器
4. 处理器决定是否发送 Telegram 消息

### 5. Storage Layer（存储层）

**数据存储：**
- **SQLite**: 结构化数据（会话、监控配置、用户偏好）
- **文件系统**: Session 数据（~/.jarvis/sessions/{chat_id}/）

**数据模型：**
- sessions: chat_id, session_id, created_at, last_active
- monitors: id, chat_id, type, config, threshold, enabled
- settings: chat_id, key, value, updated_at

## 配置管理

### 配置文件（config.yaml）
```yaml
telegram:
  token: "YOUR_BOT_TOKEN"

codex:
  workspace_dir: "~/workspace"

triggers:
  scheduler:
    - name: "daily_summary"
      cron: "0 9 * * *"
      action: "send_summary"

  monitors:
    - name: "cpu_alert"
      type: "cpu"
      threshold: 80
      interval: 60

storage:
  db_path: "~/.jarvis/jarvis.db"
  session_dir: "~/.jarvis/sessions"
```

### 动态配置（通过命令）
- `/verbosity <full|compact|result|reset>` - 输出详细程度
- `/skills ...` - 技能来源/安装管理
- `/memory ...` - 记忆写入与检索

## 实现计划

### Phase 1: 基础框架
1. 事件总线实现
2. Telegram Bot 基础集成
3. Codex CLI wrapper
4. 基础存储层

### Phase 2: 核心功能
1. Session 管理
2. 命令处理
3. 基础对话功能

### Phase 3: 触发器系统
1. Scheduler 触发器
2. Monitor 触发器
3. Webhook 触发器

### Phase 4: 高级功能
1. 技能管理
2. 记忆管理
3. 系统监控

### Phase 5: 部署和运维
1. systemd 服务配置
2. 日志和监控
3. 错误处理和恢复

## 待讨论的问题

1. Codex CLI 输出解析：如何提取 session ID？
2. Session 持久化：存储格式和恢复机制
3. 错误处理：Codex 调用失败时的重试策略
4. 监控指标：具体监控哪些系统指标？
5. Webhook 接口：如何设计 webhook 接收端点？
6. 多用户支持：是否需要支持多个 Telegram 用户？
