# US Docker Compose 部署方案

本文档说明如何把 us 服务器上的 qwen2api 从 host binary 部署迁移到 Docker Compose 部署。

## 当前目标

- 使用当前 Go 版 qwen2api 镜像运行服务。
- 继续复用服务器上的 `/opt/docker_projects/qwen2api/data` 和 `/opt/docker_projects/qwen2api/logs`。
- 继续监听宿主机 `7860`，保持 nginx-proxy 现有 `qwen2api.codeai.de5.net -> host.docker.internal:7860` 配置不变。
- 不把真实 `.env.compose`、API Key、账号 token、Cookie、镜像 tar 提交到 Git。

## Git 安全规则

只提交模板和脚本：

- `deploy/us/docker-compose.yml`
- `deploy/us/docker-compose.smoke.yml`
- `deploy/us/.env.compose.example`
- `scripts/build-docker-image.sh`
- `scripts/render-compose-env.py`
- `docs/deploy-us-docker-compose.md`

禁止提交：

- `.env.compose`
- `.env.host-dev-go`
- `.env`
- `data/`
- `logs/`
- `*.tar`
- `*.tar.gz`

提交前必须检查：

```bash
git status --short
git diff --cached --name-only
git diff --cached
git check-ignore -v ".env.compose" ".env.host-dev-go" ".env" || true
```

如果安装了 gitleaks，额外执行：

```bash
gitleaks detect --source . --redact
```

## 构建镜像

在本地项目根目录执行：

```bash
scripts/build-docker-image.sh
```

脚本默认构建：

- 平台：`linux/amd64`
- 镜像：`qwen2api:dev-go-<git短提交>`
- 导出：`/tmp/qwen2api-dev-go-<git短提交>.tar`

可选环境变量：

- `GOPROXY`：Go 模块代理，默认 `https://goproxy.cn,direct`。
- `INSTALL_BROWSERS`：是否在镜像构建阶段安装 Chromium，默认 `true`。
- `PLAYWRIGHT_VERSION`：用于生成 playwright-go driver 目录的 `playwright-core` 版本，默认 `1.57.0`。

说明：`playwright-go v0.5700.1` 需要 `1.57.0` driver。为避免 Playwright driver zip 暂不可用导致构建失败，Dockerfile 会从 npm 的 `playwright-core` 准备 driver 目录，再执行 Chromium 安装。

## 上传并加载镜像

把 tar 上传到 us 后执行：

```bash
docker load -i /tmp/qwen2api-dev-go-<git短提交>.tar
```

加载完成后删除 tar，避免占用磁盘。

## 生成服务器 .env.compose

在 us 上基于当前 host env 渲染容器路径：

```bash
python3 scripts/render-compose-env.py \
  --source /opt/docker_projects/qwen2api/.env.host-dev-go \
  --output /opt/docker_projects/qwen2api/.env.compose \
  --image-tag dev-go-<git短提交>

chmod 600 /opt/docker_projects/qwen2api/.env.compose
```

容器内路径必须是 `/app/...`，不要使用 `/opt/docker_projects/...`。

## 临时端口 smoke test

先不占用生产 `7860`：

`docker-compose.smoke.yml` 使用 Compose `!override` 显式替换 `ports` 和 `volumes`，
避免临时 smoke 容器继承生产 `7860` 端口映射。

```bash
cd /opt/docker_projects/qwen2api
docker compose \
  --env-file .env.compose \
  -f docker-compose.yml \
  -f docker-compose.smoke.yml \
  up -d

curl -fsS http://127.0.0.1:17860/healthz
curl -fsS http://127.0.0.1:17860/ | head
docker compose -f docker-compose.yml -f docker-compose.smoke.yml down
```

## 正式切换

切换前确认可回滚：

- `/opt/docker_projects/qwen2api/current-src`
- `/opt/docker_projects/qwen2api/.env.host-dev-go`
- `/opt/docker_projects/qwen2api/run-dev-go-host.sh`

切换步骤：

```bash
kill "$(cat /opt/docker_projects/qwen2api/dev-go-host.pid)"
ss -lntp | grep ':7860' || true

cd /opt/docker_projects/qwen2api
docker compose --env-file .env.compose up -d
```

## 验证

```bash
curl -fsS http://127.0.0.1:7860/healthz
docker compose --env-file .env.compose ps
docker logs --tail=100 qwen2api
```

还必须验证：

- API 文生视频 T2V。
- API 首帧图生视频 I2V。
- 页面文生视频。
- 页面首帧图生视频。
- nginx 域名访问。

## 回滚

如果 compose 失败：

```bash
cd /opt/docker_projects/qwen2api
docker compose --env-file .env.compose down

nohup /opt/docker_projects/qwen2api/run-dev-go-host.sh \
  >> /opt/docker_projects/qwen2api/logs/dev-go-host.log 2>&1 &

echo $! > /opt/docker_projects/qwen2api/dev-go-host.pid
curl -fsS http://127.0.0.1:7860/healthz
```
