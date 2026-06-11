# Claude Code 工具调用能力矩阵测试

`claude_tool_matrix.py` 用于验证 qwen2API 作为 Claude Code 第三方 Anthropic API 时，各模型是否能稳定产出并执行 Claude Code 工具调用。

## 安全默认值

- 默认不请求线上服务、不读取 API Key、不启动 Claude Code；只打印测试计划。
- 真实执行必须显式传入 `--execute`。
- API Key 只从环境变量读取，默认按顺序读取：`ANTHROPIC_API_KEY`、`QWEN2API_API_KEY`。
- 所有测试文件、日志和结果默认写入 `/tmp/qwen2api-claude-tool-matrix-*`。

## 快速预览

```bash
python3 tools/claude_tool_matrix.py --models qwen3.6-plus --include-p1
```

## 真实执行单模型冒烟

```bash
export ANTHROPIC_API_KEY="sk-..."
python3 tools/claude_tool_matrix.py \
  --execute \
  --models qwen3.6-plus \
  --tests Read,Write,Bash \
  --verbose
```

## 真实执行完整文本模型矩阵

```bash
export ANTHROPIC_API_KEY="sk-..."
python3 tools/claude_tool_matrix.py \
  --execute \
  --api-base https://qwen2api.codeai.de5.net \
  --include-p1 \
  --include-aliases \
  --verbose
```

## 输出文件

每次运行会生成：

- `manifest.json`：测试配置、模型和场景清单，不包含 API Key。
- `results.jsonl`：逐模型逐场景结构化结果。
- `summary.md`：按模型汇总的 Markdown 报告。
- `cases/<model>/<scenario>/attempt-*`：单场景工作目录和 Claude Code stdout/stderr。

## 判定含义

- `PASS`：预期工具事件出现，参数和副作用正确，最终回答消费了 tool_result。
- `PARTIAL`：工具或副作用成功，但事件解析或最终回答不完整。
- `FAIL`：未调用工具、参数错误、执行失败或副作用不正确。
- `UNSTABLE`：重试后状态或失败原因不一致。
- `SKIP`：模型或场景不适合本轮工具调用测试。
