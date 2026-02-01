# 部署指南（VPS / systemd）

## 1. 准备目录与用户

```bash
sudo useradd -r -s /usr/sbin/nologin jarvis
sudo mkdir -p /opt/jarvis /etc/jarvis /var/lib/jarvis /var/log/jarvis
sudo chown -R jarvis:jarvis /opt/jarvis /var/lib/jarvis /var/log/jarvis
```

## 2. 上传代码与安装依赖

你可以使用一键安装脚本（推荐）：

```bash
sudo /opt/jarvis/scripts/install.sh
```

或手动安装：

```bash
sudo -u jarvis git clone <YOUR_REPO_URL> /opt/jarvis
cd /opt/jarvis
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e .
```

## 3. 配置

```bash
sudo cp /opt/jarvis/config.sample.yaml /etc/jarvis/config.yaml
sudo cp /opt/jarvis/.env.example /etc/jarvis/jarvis.env
sudo chown jarvis:jarvis /etc/jarvis/config.yaml /etc/jarvis/jarvis.env
```

编辑 `/etc/jarvis/config.yaml` 和 `/etc/jarvis/jarvis.env`，填入 Telegram token、workspace 路径等。

> 建议将敏感信息写入 `/etc/jarvis/jarvis.env`，systemd 会自动加载。

## 4. 安装 systemd 服务

```bash
sudo cp /opt/jarvis/deploy/jarvis.service /etc/systemd/system/jarvis.service
sudo systemctl daemon-reload
sudo systemctl enable jarvis
sudo systemctl start jarvis
```

## 7. 测试（可选）

```bash
/opt/jarvis/scripts/smoke_test.sh
```

## 5. 查看日志

```bash
sudo journalctl -u jarvis -f
# 或者查看文件日志（若配置了 logging.file）
ls -la /var/log/jarvis
```

## 6. Webhook（可选）

若使用 webhook：

- 端口默认 `8080`，可通过 `WEBHOOK_PORT` 或 `config.yaml` 修改
- 令牌通过 `WEBHOOK_TOKEN` 设置，客户端需在请求头带 `X-Webhook-Token`

示例：

```bash
curl -X POST http://your-host:8080/webhook \
  -H "X-Webhook-Token: <token>" \
  -H "Content-Type: application/json" \
  -d '{"chat_id":"<chat_id>","message":"hello from webhook"}'
```
