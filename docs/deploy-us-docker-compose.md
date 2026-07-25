# US Docker Compose 蓝绿部署方案

us 生产服务器只负责加载镜像和启动容器，禁止执行源码构建。镜像必须在本地构建、导出、压缩后上传。

## 拓扑

- blue：`172.17.0.1:17863`
- green：`172.17.0.1:17864`
- `qwen2api-router`：固定监听 `172.17.0.1:7860`、`172.17.0.1:17861` 和 `172.17.0.1:17862`，统一转发到当前活动槽
- `nginx-proxy`：不可变外部依赖，只访问 router 的稳定兼容入口，部署不得修改、reload、restart 或 recreate
- 数据和日志：继续挂载 `/opt/docker_projects/qwen2api/data`、`/opt/docker_projects/qwen2api/logs`

候选容器健康后才切换 qwen2API 自有 router；三个稳定入口和公网验证通过后停止旧容器。失败时恢复 router 配置、重新启动已停止的旧槽并停止候选容器。候选容器延迟 180 秒启动 token 刷新任务，避免蓝绿短暂并行期间同时写账号文件。

内部调用方只能使用稳定地址 `http://172.17.0.1:7860`。`17861` 和 `17862` 是兼容不可变外部代理的稳定 router 入口；任何依赖方都不得直接引用槽位端口 `17863` 或 `17864`。

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
6. 备份并切换服务自有的 `qwen2api-router`。
7. 验证稳定入口 `172.17.0.1:7860`、`17861` 和 `17862`。
8. 验证公网 `/healthz`；失败自动恢复 qwen2API router 并停止候选容器。
9. 验证成功后停止旧容器、记录活动槽并删除 router 临时备份。
10. 本地退出时删除构建归档；整个流程不操作 `nginx-proxy`。

认证由 SSH config、ssh-agent 或调用者环境提供，不得写入脚本或 Git。

## 可选参数

```text
US_DEPLOY_DIR=/opt/docker_projects/qwen2api
US_PUBLIC_HEALTH_URL=https://qwen2api.codeai.de5.net/healthz
UPLOAD_CHUNK_SIZE=32m
```

槽位端口 `17863` 和 `17864`、router 稳定端口 `7860/17861/17862` 是 us 拓扑常量，不通过环境变量覆盖。

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
curl -fsS http://172.17.0.1:17861/healthz
curl -fsS http://172.17.0.1:17862/healthz
curl -fsS https://qwen2api.codeai.de5.net/healthz
```

切换后还应真实验证 OpenAI、Responses、Anthropic 三种 LLM 协议，以及图片和视频端点。

## 手动回滚

假设需要回滚到 blue：

```bash
docker start qwen2api-blue
curl -fsS http://172.17.0.1:17863/healthz
```

确认 blue 健康后，只切换 qwen2API 自有 router：

```bash
python3 - <<'PY'
from pathlib import Path

base = Path("/opt/docker_projects/qwen2api")
template = (base / "qwen2api-router.conf.template").read_text()
(base / "qwen2api-router.conf").write_text(
    template.replace("__QWEN2API_TARGET_PORT__", "17863")
)
PY
docker exec qwen2api-router nginx -t
docker exec qwen2api-router nginx -s reload
curl -fsS http://172.17.0.1:7860/healthz
curl -fsS http://172.17.0.1:17861/healthz
curl -fsS http://172.17.0.1:17862/healthz
curl -fsS https://qwen2api.codeai.de5.net/healthz
printf 'blue\n' > /opt/docker_projects/qwen2api/.active-slot
docker stop qwen2api-green
```

回滚到 green 时把 router 目标端口改为 `17864`，步骤相同。回滚过程同样不得操作 `nginx-proxy`。

## 数据一致性边界

当前服务使用本地 JSON 文件保存账号状态，不适合两个实例长期同时处理流量。蓝绿重叠仅用于启动和健康检查，切流成功后立即停止旧槽；部署窗口内不要执行账号批量导入、删除或管理端写操作。若未来需要长期双活，应先迁移到支持并发写入的数据库。
