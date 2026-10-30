# NapCat GitHub Monitor

从 Yumi QQ 机器人中独立提取的 GitHub 仓库监控。Python 3.10+，通过 NapCat / OneBot v11 HTTP 接口向 QQ 群和私聊推送本地生成的 1200×600 图片。

支持 Push、Release、Issue、PR，SQLite 订阅持久化、事件去重、发送失败退避重试、管理员权限检查，以及独立命令队列。无需原机器人的 AI、音乐、表情服务。

## 启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填写 NapCat 地址、访问令牌、上报 secret 和机器人管理员 QQ
set -a
. ./.env
set +a
python app.py
```

Windows 可用 `.venv\Scripts\Activate.ps1` 激活，通过 `$env:ONEBOT_SECRET='...'` 等设置环境变量，再执行 `python app.py`。程序不会自动加载 `.env`。

Linux 安装中文字体：`sudo apt-get install fonts-noto-cjk`。Windows 默认使用微软雅黑。

在 NapCat 开启 HTTP 服务，并把 HTTP 客户端上报地址配置为 `http://监控服务地址:8088/onebot/events`。上报 secret 必须与 `ONEBOT_SECRET` 一致；NapCat HTTP 服务 token 对应 `ONEBOT_ACCESS_TOKEN`。健康检查为 `GET /health`。

默认仅监听 127.0.0.1；跨机器连接时设置 `HOST=0.0.0.0` 并配置网络访问权限。图片用 Base64 传输，NapCat 无需共享文件目录。请只运行一个服务进程，避免重复轮询。

## QQ 命令

- `订阅 https://github.com/owner/repo`
- `GitHub订阅 owner/repo`
- `GitHub退订 owner/repo`
- `GitHub订阅列表`
- `GitHub帮助`

支持 Markdown 仓库链接。群内订阅绑定当前群，只有群主、群管理员和 `ADMIN_QQ` 可以修改群订阅；私聊订阅绑定发送者。

## Docker

```bash
docker build -t napcat-github-monitor .
docker run -d --name github-monitor --env-file .env -e HOST=0.0.0.0 -p 8088:8088 -v github-monitor-data:/app/output napcat-github-monitor
```

容器内的 `ONEBOT_HTTP_URL` 必须填写容器可访问的 NapCat 地址，不能用宿主机的 127.0.0.1；Docker Desktop 可使用 `host.docker.internal`。

## 行为和限制

- 每 5 分钟检查，最多监控 4 个不同仓库；多个目标订阅同一仓库共享轮询。
- `GITHUB_TOKEN` 为可选 GitHub API 令牌，可提高请求额度；兼容原 `YUMI_GITHUB_TOKEN`。
- 新订阅不发送历史。每次重启仅发送启动之后的事件，清空上次运行的待发送队列，但保留订阅。
- 本次运行中的失败发送按 30 秒至 1 小时退避重试。
- Events API 有延迟，仅获取最新 100 条事件，高活跃仓库可能漏事件；面向公开仓库，不保证完整审计或实时通知。
- 状态默认存于 `output/github-monitor.sqlite3`，头像缓存和临时卡片也在 `output/`。

## 从原机器人迁移

先停止原 GitHub 监控，再使用 SQLite backup 将原 `output/github-monitor.sqlite3` 复制到本项目对应位置；数据库未随仓库发布。若两边同时保留相同订阅，会重复通知。原机器人代码未因本次提取而删除或停用。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖事件去重、启动基线、订阅隔离、失败重试、群权限、中文卡片、头像降级、提交信息补充以及 OneBot 上报验签和图片消息封装。测试不发送真实 QQ 消息。
