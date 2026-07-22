# qwen2API 项目约定

## us 生产部署

- 所有 us 服务器部署必须使用 `scripts/deploy-us-image.sh` 执行蓝绿部署，禁止直接覆盖当前容器或原地重启切换版本。
- 镜像必须在本地构建为 `linux/amd64` 后上传；禁止在 us 生产服务器构建镜像。
- 候选槽通过内部 `/healthz` 后才能切换 nginx；公网 `/healthz` 验证成功后才能停止旧槽。
- 切换失败必须自动恢复 nginx 并停止候选槽；保留当前版本和上一版本用于回滚。
- 部署日志、报错和命令输出不得包含密码、私钥、API Key、Token、Cookie、Authorization、完整请求头或真实 `.env` 内容；诊断时只输出配置键名、状态和脱敏摘要。
- 真实 `.env`、账号数据、日志和镜像归档不得提交到 Git。
