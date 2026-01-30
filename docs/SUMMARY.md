# Jarvis 项目总结

## 项目概述

Jarvis 是一个基于 Codex SDK 的 Telegram 个人助理 Agent，实现了长期在线运行、双向通信、智能对话、任务管理、提醒系统和系统监控等功能。

## 实现的功能

### 核心功能
✅ **事件驱动架构**：中央事件总线协调所有组件
✅ **Telegram Bot 集成**：消息收发、命令处理
✅ **Codex CLI 封装**：会话管理、JSONL 解析、重试机制
✅ **会话管理**：独立会话、/reset、/compact
✅ **数据持久化**：SQLite + 文件存储

### 高级功能
✅ **任务管理**：添加、查看、完成任务
✅ **提醒系统**：定时提醒、列表查看、取消提醒
✅ **触发器系统**：
  - Scheduler：定时任务（cron 表达式）
  - Monitor：系统监控（CPU、内存、磁盘、负载）
  - Webhook：外部事件接收

### 部署支持
✅ **systemd 服务配置**
✅ **一键安装脚本**
✅ **环境变量支持**
✅ **日志轮转**
✅ **烟雾测试脚本**

## 技术亮点

### 1. 事件驱动架构
- 组件解耦，易于扩展
- 异步事件处理
- 错误隔离

### 2. Codex CLI 集成
- 无需官方 Python SDK
- JSONL 事件流解析
- Session 自动管理
- 重试和超时处理

### 3. 灵活的触发器系统
- 时间触发（APScheduler）
- 系统监控（psutil）
- Webhook 接收（aiohttp）
- 统一事件接口

### 4. 生产就绪
- systemd 服务管理
- 日志轮转
- 安全配置（NoNewPrivileges、PrivateTmp）
- 环境变量支持

## 项目结构

```
jarvis/
├── jarvis/                      # 主代码
│   ├── __main__.py             # 入口
│   ├── app.py                  # 应用主逻辑
│   ├── event_bus.py            # 事件总线
│   ├── config.py               # 配置管理
│   ├── codex/                  # Codex 管理
│   │   └── manager.py          # CLI 封装、JSONL 解析
│   ├── telegram/               # Telegram Bot
│   │   └── bot.py              # 消息收发、命令路由
│   ├── triggers/               # 触发器系统
│   │   ├── manager.py          # 触发器管理器
│   │   ├── scheduler.py        # 定时任务
│   │   ├── monitor.py          # 系统监控
│   │   └── webhook.py          # Webhook 服务器
│   └── storage/                # 存储层
│       └── db.py               # SQLite 操作
├── docs/                       # 文档
│   ├── plans/                  # 设计文档
│   ├── deploy.md               # 部署指南
│   └── SUMMARY.md              # 项目总结
├── scripts/                    # 工具脚本
│   ├── install.sh              # 一键安装
│   ├── run.sh                  # 本地运行
│   └── smoke_test.sh           # 烟雾测试
├── deploy/                     # 部署配置
│   └── jarvis.service          # systemd 服务
├── config.sample.yaml          # 配置示例
├── .env.example                # 环境变量示例
├── pyproject.toml              # 项目配置
└── README.md                   # 项目说明
```

## 核心组件说明

### Event Bus（事件总线）
- 异步 pub/sub 模式
- 错误隔离（单个 handler 失败不影响其他）
- 事件类型：
  - `telegram.message_received`
  - `telegram.send_message`
  - `telegram.command`
  - `trigger.fired`

### Codex Manager
- 封装 `codex exec --json` 调用
- 解析 JSONL 事件流
- 提取 `thread_id` 和响应文本
- 支持 `resume` 恢复会话
- 重试机制（指数退避）

### Telegram Bot Manager
- 基于 python-telegram-bot
- 命令路由（/start、/help、/reset、/compact）
- 消息过滤
- 事件发布

### Trigger System
- **Scheduler**：APScheduler，支持 cron 表达式
- **Monitor**：psutil，监控 CPU、内存、磁盘、负载
- **Webhook**：aiohttp，接收外部事件

### Storage Layer
- SQLite 数据库
- 表结构：
  - `sessions`：会话管理
  - `monitors`：监控配置
  - `settings`：用户偏好
- 异步操作（aiosqlite）

## 使用场景

### 1. 日常助手
- 通过 Telegram 与 Codex 对话
- 查询信息、生成代码、解决问题

### 2. 任务管理
- 添加待办事项
- 查看任务列表
- 标记完成

### 3. 定时提醒
- 设置提醒时间
- 自动推送通知

### 4. 系统监控
- 监控服务器状态
- CPU/内存/磁盘告警
- 自动推送告警消息

### 5. 事件通知
- 接收 Webhook 事件
- 转发到 Telegram
- 自定义处理逻辑

## 部署方式

### 开发环境
```bash
pip install -e .
python -m jarvis --config config.yaml
```

### 生产环境（VPS）
```bash
sudo /opt/jarvis/scripts/install.sh
sudo systemctl status jarvis
```

## 配置示例

### 基础配置
```yaml
telegram:
  token: "YOUR_BOT_TOKEN"

codex:
  workspace_dir: "~/workspace"
  timeout_seconds: 120

storage:
  db_path: "~/.jarvis/jarvis.db"
```

### 触发器配置
```yaml
triggers:
  scheduler:
    - name: "morning_greeting"
      cron: "0 9 * * *"
      action: "send_greeting"

  monitors:
    - name: "cpu_alert"
      type: "cpu"
      threshold: 80
      interval: 60
```

## 扩展性

### 添加新的触发器类型
1. 在 `jarvis/triggers/` 创建新模块
2. 实现触发逻辑
3. 发布 `trigger.fired` 事件
4. 在 `TriggerManager` 中注册

### 添加新的命令
1. 在 `jarvis/app.py` 的 `_on_command` 中添加处理逻辑
2. 在 `jarvis/telegram/bot.py` 中注册命令 handler

### 添加新的存储表
1. 在 `jarvis/storage/db.py` 中添加表结构
2. 实现 CRUD 方法

## 未来改进方向

### 功能增强
- [ ] 自然语言时间解析（"明天 9 点"）
- [ ] 任务标签和分类
- [ ] 任务优先级和截止日期
- [ ] 更丰富的监控指标（网络、IO）
- [ ] 多用户权限管理

### 技术优化
- [ ] 单元测试覆盖
- [ ] 集成测试
- [ ] 性能优化
- [ ] 更好的错误恢复机制
- [ ] 配置热重载

### 部署改进
- [ ] Docker 支持
- [ ] Docker Compose 一键部署
- [ ] 健康检查端点
- [ ] Prometheus metrics

## 依赖项

```toml
dependencies = [
  "python-telegram-bot>=21.0",
  "aiosqlite>=0.20.0",
  "PyYAML>=6.0.1",
  "APScheduler>=3.10.0",
  "aiohttp>=3.9.0",
  "psutil>=5.9.0",
]
```

## 许可证

MIT License

## 致谢

- Codex CLI：提供强大的 AI 能力
- python-telegram-bot：优秀的 Telegram Bot 框架
- APScheduler：灵活的定时任务调度
