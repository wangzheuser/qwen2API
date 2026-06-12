# Claude Code 第三方 API 工具调用能力测试结果

- 作者：wangqiupei
- 测试日期：2026-06-12
- 网关地址：`https://qwen2api.codeai.de5.net`
- Claude Code：`2.1.170`
- 测试脚本：`tools/claude_tool_matrix.py`
- 主测试场景：`CoreChain`，单会话验证 `Read -> Edit -> Write -> Bash`

## 结论摘要

- chat 基础模型与 Claude 别名核心链路：25 个通过，2 个失败。
- 推荐优先使用通过 `CoreChain` 的 chat 基础模型，尤其是 `qwen3.7-plus`、`qwen3.7-max`、`qwen3.6-plus`、`qwen3-coder-plus`、`qwen3.5-flash`。
- 不建议当前作为 Claude Code 工具模型使用：`qwen3-omni-flash-2025-12-01`、`claude-sonnet-4-6`。
- `deep_research` 变体不适合本轮工具链路；`qwen3.7-plus-deep-research` 已验证未产生工具调用。

## 测试环境与工具面

- 协议入口：Anthropic 兼容入口 `/anthropic`
- 隔离模式：Claude Code `--safe-mode`
- 权限模式：`acceptEdits`
- API Key：通过环境变量传入，未写入报告和日志摘要
- 结果目录：
  - chat 核心链路：`/private/tmp/qwen2api-claude-tool-matrix-chat-core-20260612-100353`
  - 变体抽样：`/private/tmp/qwen2api-claude-tool-matrix-variants-core-20260612-102157`

Claude Code safe-mode 初始化暴露的工具：

```text
Task, AskUserQuestion, Bash, CronCreate, CronDelete, CronList, Edit, EnterPlanMode, EnterWorktree, ExitPlanMode, ExitWorktree, NotebookEdit, Read, ScheduleWakeup, Skill, TaskCreate, TaskGet, TaskList, TaskOutput, TaskStop, TaskUpdate, WebFetch, WebSearch, Workflow, Write
```

本机 Claude Code 2.1.170 safe-mode 未暴露 `LS / Glob / Grep / MultiEdit / TodoWrite`，相关场景在脚本中会标记为 `SKIP`，不计为模型失败。

## chat 基础模型 CoreChain 全量结果

| 模型 | 状态 | 观察到的工具 | 结论 |
|---|---|---|---|
| `qwen3.7-plus` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.7-max` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.6-plus` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.6-max-preview` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.6-27b` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen-latest-series-invite-beta-v24` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen-latest-series-invite-beta-v16` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.5-plus` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.5-omni-plus` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.6-35b-a3b` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.5-flash` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.5-max-2026-03-08` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.6-plus-preview` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.5-397b-a17b` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.5-122b-a10b` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.5-omni-flash` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.5-27b` | PASS | Read, Edit, Write, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.5-35b-a3b` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3-max-2026-01-23` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen-plus-2025-07-28` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3-coder-plus` | PASS | Read, Edit, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3-vl-plus` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3-omni-flash-2025-12-01` | FAIL | Read, Edit, Write | CoreChain 缺少工具调用：Bash |
| `claude-sonnet-4-5` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `claude-sonnet-4-6` | FAIL | - | CoreChain 缺少工具调用：Bash, Edit, Read, Write |
| `claude-opus-4-6` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `claude-3-haiku` | PASS | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |

### 通过模型

- `qwen3.7-plus`
- `qwen3.7-max`
- `qwen3.6-plus`
- `qwen3.6-max-preview`
- `qwen3.6-27b`
- `qwen-latest-series-invite-beta-v24`
- `qwen-latest-series-invite-beta-v16`
- `qwen3.5-plus`
- `qwen3.5-omni-plus`
- `qwen3.6-35b-a3b`
- `qwen3.5-flash`
- `qwen3.5-max-2026-03-08`
- `qwen3.6-plus-preview`
- `qwen3.5-397b-a17b`
- `qwen3.5-122b-a10b`
- `qwen3.5-omni-flash`
- `qwen3.5-27b`
- `qwen3.5-35b-a3b`
- `qwen3-max-2026-01-23`
- `qwen-plus-2025-07-28`
- `qwen3-coder-plus`
- `qwen3-vl-plus`
- `claude-sonnet-4-5`
- `claude-opus-4-6`
- `claude-3-haiku`

### 失败模型

- `qwen3-omni-flash-2025-12-01`：CoreChain 缺少工具调用：Bash
- `claude-sonnet-4-6`：CoreChain 缺少工具调用：Bash, Edit, Read, Write

## 变体模型抽样结果

- 抽样结果：2 个通过，1 个失败。
- 该批次因 `deep_research` 场景耗时长且已出现工具调用失败样本，已手动中断以避免继续消耗额度。

| 模型 | 状态 | 耗时 | 观察到的工具 | 结论 |
|---|---|---:|---|---|
| `qwen3.7-plus-thinking` | PASS | 107.2s | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.7-plus-search` | PASS | 29.5s | Read, Edit, Write, Bash | CoreChain 核心工具链成功 |
| `qwen3.7-plus-deep-research` | FAIL | 227.0s | - | CoreChain 缺少工具调用：Bash, Edit, Read, Write |

## 单工具与脚本校准结果

- `qwen3.6-plus` 单工具校准：`Read`、`Write`、`Edit` 均已单独验证通过。
- `Edit` 工具要求目标文件先进入上下文，因此脚本中的 Edit 场景采用 `Read -> Edit`。
- `Bash` 单工具曾出现工具调用成功但最终回答未消费 stdout 的 `PARTIAL`，因此批量判断以 `CoreChain` 的 Bash 校验和最终 marker 为准。

## 推荐使用方式

快速复测全部 chat 基础模型核心工具链：

```bash
export ANTHROPIC_API_KEY="sk-..."
python3 tools/claude_tool_matrix.py \
  --execute \
  --api-base https://qwen2api.codeai.de5.net \
  --modes chat \
  --tests CoreChain \
  --include-aliases \
  --verbose
```

单模型完整可用工具场景复测：

```bash
export ANTHROPIC_API_KEY="sk-..."
python3 tools/claude_tool_matrix.py \
  --execute \
  --api-base https://qwen2api.codeai.de5.net \
  --models qwen3.6-plus \
  --include-p1 \
  --verbose
```

## 注意事项

- 结果受 Claude Code 版本、隔离模式、qwen2API 上游账号状态和模型当前行为影响。
- 本报告不包含 API Key；原始 stdout/stderr 位于 `/private/tmp/qwen2api-claude-tool-matrix-*`。
- 若升级 Claude Code 后工具清单发生变化，应重新运行矩阵。
