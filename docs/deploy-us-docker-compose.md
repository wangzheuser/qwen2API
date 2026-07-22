# US Docker Compose 蓝绿部署方案

us 生产服务器只负责加载镜像和启动容器，禁止执行源码构建。镜像必须在本地构建、导出、压缩后上传。

## 拓扑

- blue：`172.17.0.1:17861`
- green：`172.17.0.1:17862`
- `qwen2api-router`：固定监听 `172.17.0.1:7860`，转发到当前活动槽，供 New API 等内部服务使用
- `nginx-proxy`：把 `qwen2api.codeai.de5.net` 切换到当前活动端口
- 数据和日志：继续挂载 `/opt/docker_projects/qwen2api/data`、`/opt/docker_projects/qwen2api/logs`

候选容器健康后才修改 nginx；公网验证通过后停止旧容器。失败时恢复 nginx 配置并停止候选容器。候选容器延迟 180 秒启动 token 刷新任务，避免蓝绿短暂并行期间同时写账号文件。

内部调用方只能使用稳定地址 `http://172.17.0.1:7860`，不得直接引用 `17861` 或 `17862`。部署脚本会同步切换服务自有的 `qwen2api-router`，因此内部配置不随槽位变化。

## Git 安全

可提交的部署文件：

- `deploy/us/docker-compose.yml`
- `deploy/us/docker-compose.blue-green.yml`
- `deploy/us/docker-compose.router.yml`
- `deploy/us/qwen2api-router.conf.template`
- `deploy/us/docker-compose.smoke.yml`
- `deploy/us/.env.compose.example`
- `scripts/build-docker-image.sh`
- `scripts/deploy-us-image.sh`
- `scripts/render-compose-env.py`

禁止提交 `.env*`、`data/`、`logs/`、镜像归档、API Key、账号 token、Cookie、密码和私钥。

提交前检查：

```bash
git status --short
git diff --cached
git check-ignore -v .env .env.compose || true
gitleaks detect --source . --redact
```

## 本地构建与部署

构建机需要 Docker、`zstd`、SSH，并至少预留 8–12GB 可用空间。配置 SSH alias `us` 后执行：

```bash
CONFIRM_PRODUCTION_DEPLOY=yes scripts/deploy-us-image.sh
```

显式指定标签：

```bash
CONFIRM_PRODUCTION_DEPLOY=yes scripts/deploy-us-image.sh dev-go-<git短提交>
```

没有 SSH alias 时：

```bash
US_SSH_TARGET=root@example.com \
US_SSH_PORT=22 \
CONFIRM_PRODUCTION_DEPLOY=yes \
scripts/deploy-us-image.sh
```

脚本按以下顺序执行：

1. 本地构建 `linux/amd64` 镜像。
2. 本地以流式方式执行 `docker save | zstd`，避免同时占用 tar 和压缩包两份磁盘空间。
3. 把压缩镜像拆成小分片逐个 SCP 上传，并在 us 合并；单次连接中断不会产生不可识别的半包镜像。
4. us 执行 `docker load`，立即删除上传归档。
5. 在非活动槽启动候选容器并检查 `/healthz`。
6. 把服务自有的 `qwen2api-router` 切到候选槽，验证稳定内部入口 `172.17.0.1:7860`。
7. 备份公网 nginx 配置、修改 qwen2api server block、执行 `nginx -t` 和 reload。
8. 验证公网 `/healthz`；失败自动恢复内部路由和公网 nginx，并停止候选容器。
9. 验证成功后记录活动槽、停止旧容器并删除临时备份。
10. 本地退出时删除构建归档。

认证由 SSH config、ssh-agent 或调用者环境提供，不得写入脚本或 Git。

## 可选参数

```text
US_DEPLOY_DIR=/opt/docker_projects/qwen2api
US_BLUE_PORT=17861
US_GREEN_PORT=17862
US_PROXY_CONFIG=/opt/docker_projects/nginx-proxy/nginx.conf
US_PROXY_CONTAINER=nginx-proxy
US_PUBLIC_HEALTH_URL=https://qwen2api.codeai.de5.net/healthz
UPLOAD_CHUNK_SIZE=32m
```

若目标标签镜像已经在本地构建完成，可跳过重复构建和远端镜像元数据请求：

```bash
SKIP_LOCAL_BUILD=true \
CONFIRM_PRODUCTION_DEPLOY=yes \
scripts/deploy-us-image.sh dev-go-<git短提交>
```

## 手动验证

```bash
cat /opt/docker_projects/qwen2api/.active-slot
docker ps --filter name=qwen2api-
curl -fsS http://172.17.0.1:7860/healthz
docker exec nginx-proxy nginx -t
curl -fsS https://qwen2api.codeai.de5.net/healthz
```

切换后还应真实验证 OpenAI、Responses、Anthropic 三种 LLM 协议，以及图片和视频端点。

## 手动回滚

假设需要回滚到 blue：

```bash
docker start qwen2api-blue
curl -fsS http://172.17.0.1:17861/healthz
```

确认 blue 健康后，仅把 nginx 中 qwen2api server block 的两处 `proxy_pass` 改为：

```nginx
proxy_pass http://host.docker.internal:17861;
```

然后执行：

```bash
docker exec nginx-proxy nginx -t
docker exec nginx-proxy nginx -s reload
curl -fsS https://qwen2api.codeai.de5.net/healthz
printf 'blue\n' > /opt/docker_projects/qwen2api/.active-slot
docker stop qwen2api-green
```

回滚到 green 时使用端口 `17862`，步骤相同。

## 数据一致性边界

当前服务使用本地 JSON 文件保存账号状态，不适合两个实例长期同时处理流量。蓝绿重叠仅用于启动和健康检查，切流成功后立即停止旧槽；部署窗口内不要执行账号批量导入、删除或管理端写操作。若未来需要长期双活，应先迁移到支持并发写入的数据库。
