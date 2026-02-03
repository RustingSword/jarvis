# Artifact 生成与分享（R2 + Worker）设计

## 目标
- 生成任意 artifacts（PDF/HTML/音视频等）并自动上传分享
- 使用 `*.euwyn.toonaive.me` 三级域名短链接
- 支持公开访问，允许可选过期
- 低访问量、低维护成本，易扩展

## 范围与非目标
- 本期不做登录鉴权（公开分享）
- 不做复杂内容审查或敏感识别
- 不引入多租户/团队权限模型

## 架构概览
- **Jarvis 生成侧**：产物落地到本地路径
- **发布器（Jarvis 内）**：上传到 R2，记录元数据，返回分享 URL
- **Cloudflare Worker**：统一入口，解析子域名并返回对象，执行过期检查
- **Cloudflare R2**：对象存储
- **DNS/证书**：Cloudflare 通配 `*.euwyn.toonaive.me`

## 组件与职责
### 1) Artifact 发布器（Jarvis 内）
- 输入：本地文件路径、可选标题/过期时间、可选 content-type
- 生成：`artifact_id`（短 ID）、`r2_key`（`artifacts/{id}/{filename}`）
- 上传：R2 S3 兼容 API
- 记录：元数据（SQLite/现有 storage）
- 输出：分享链接 `https://{id}.euwyn.toonaive.me`

**建议元数据字段**
- `id`、`r2_key`、`content_type`、`size`
- `created_at`、`expires_at`（可空）
- `title`（可空）、`source_task`

### 2) Cloudflare Worker
- 解析请求 `Host` 获取 `id`
- 读取元数据并检查过期
- 从 R2 读取对象并流式返回
- 对于过期/不存在：返回 404 或 410
- 可选：对 PDF/视频返回轻量预览页

### 3) Cloudflare R2
- 存储 artifacts
- 通过 Worker 统一访问（避免直出）

## 数据流
1. Jarvis 生成 artifact 本地文件
2. 调用发布器：生成 `id` 与 `r2_key`
3. 上传至 R2，写入元数据
4. 返回分享链接给 Telegram
5. 访问 `https://{id}.euwyn.toonaive.me` → Worker 校验 → R2 返回内容

## 过期与清理
- **访问层**：Worker 校验 `expires_at`，过期直接拒绝
- **清理层**：Jarvis 定时任务扫描元数据，删除过期对象和记录

## 错误处理与可观测性
- 上传失败：记录 `failed` 状态，返回错误，不生成链接
- Worker 404/410：返回轻量提示页
- 记录日志：
  - Jarvis：发布/清理日志（id、size、expires_at）
  - Worker：命中/过期/缺失

## 配置建议
- R2 凭证通过环境变量（不改 `config.yaml`）
  - `R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`、`R2_BUCKET`
- Worker 绑定域名：`*.euwyn.toonaive.me`
- 限制对象大小（默认 200MB）

## 测试计划
- 单元：ID 生成、过期判断、元数据写入
- 集成：上传 → 访问 200；过期 → 404/410
- 回归：现有 PDF/HTML 生成流程无改动

## 落地步骤（最小可执行）
1. Cloudflare 创建 R2 bucket
2. 创建 Worker 并绑定 `*.euwyn.toonaive.me`
3. Jarvis 新增发布器模块与清理任务
4. 在现有生成流程末尾调用发布器并返回链接

## 风险与回滚
- 风险：Worker 逻辑错误导致 404；R2 凭证错误导致上传失败
- 回滚：保留原本地文件路径与回传，禁用发布器调用即可
