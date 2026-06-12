# qwen2api 服务接口调用指南

> 作者：wangqiupei  
> 适用对象：需要从其它项目调用当前 qwen2api 服务的后端、前端或脚本开发者  
> 默认服务地址：`https://qwen2api.codeai.de5.net`  
> OpenAI 兼容 Base URL：`https://qwen2api.codeai.de5.net/v1`  

本文说明如何调用当前项目提供的模型查询、对话、生成图片、下载图片、生成视频、下载视频能力。文档中的 `YOUR_API_KEY` 为占位符，请替换为 qwen2api WebUI 中创建的客户端 API Key。

## 1. 接入基础约定

### 1.1 服务地址

| 项目 | 地址 |
|---|---|
| WebUI 管理台 | `https://qwen2api.codeai.de5.net/` |
| OpenAI 兼容 Base URL | `https://qwen2api.codeai.de5.net/v1` |
| 模型列表 | `GET https://qwen2api.codeai.de5.net/v1/models` |
| 单模型信息 | `GET https://qwen2api.codeai.de5.net/v1/models/{model_id}` |
| 对话接口 | `POST https://qwen2api.codeai.de5.net/v1/chat/completions` |
| 图片生成 | `POST https://qwen2api.codeai.de5.net/v1/images/generations` |
| 视频生成 | `POST https://qwen2api.codeai.de5.net/v1/videos/generations` |

### 1.2 鉴权方式

推荐使用标准 Bearer Token：

```http
Authorization: Bearer YOUR_API_KEY
```

图片和视频接口也兼容：

```http
x-api-key: YOUR_API_KEY
```

生产环境不建议把密钥放在 URL 查询参数中，避免进入日志、浏览器历史或代理缓存。

### 1.3 请求格式

除下载图片、下载视频外，本文接口均使用 JSON 请求体：

```http
Content-Type: application/json
```

图片和视频下载直接访问生成接口返回的 `data[].url`，通常使用 `curl -L`、`requests.get` 或浏览器/前端下载逻辑。

## 2. qwen 系列模型信息

### 2.1 模型列表来源和运行时规则

`GET /v1/models` 会优先通过账号池从 Qwen 上游 `/api/models` 获取实时模型列表，然后由 qwen2api 按模型能力自动扩展模式变体。所以下表是当前项目快照中的完整 qwen 系列基础模型信息，实际生产调用应以运行时 `/v1/models` 返回为准。

如果上游模型列表暂时不可用，服务会回退到内置兼容别名表，例如 `gpt-4o -> qwen3.6-plus`、`gpt-4o-mini -> qwen3.5-flash`。

### 2.2 基础模型总览

| 模型 ID | 展示名 | family | 支持能力 | 推荐场景 |
|---|---|---|---|---|
| `qwen3.7-plus` | Qwen3.7-Plus | `qwen3.7` | 思考、搜索、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | 通用首选；能力覆盖完整，适合文本、图片、视频和多能力接入。 |
| `qwen3.7-max` | Qwen3.7-Max | `qwen3.7` | 思考、深度研究、图片生成、视频生成、WebDev、幻灯片 | 高质量通用模型，适合质量优先的文本、图片和视频任务。 |
| `qwen3.6-plus` | Qwen3.6-Plus | `qwen3.6` | 思考、搜索、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | README 默认模型；稳定通用，兼容性好。 |
| `qwen3.6-max-preview` | Qwen3.6-Max-Preview | `qwen3.6` | 思考、深度研究、WebDev、幻灯片 | 高质量预览模型，适合文本、深度研究、WebDev、幻灯片。 |
| `qwen3.6-27b` | Qwen3.6-27B | `qwen3.6` | 思考、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | 中大型通用模型，支持视觉、图片、视频等多能力。 |
| `qwen-latest-series-invite-beta-v24` | Qwen3.7-Max-Preview | `qwen` | 思考、WebDev、幻灯片 | 邀请/预览系列，适合试验文本、WebDev、幻灯片能力。 |
| `qwen-latest-series-invite-beta-v16` | Qwen3.7-Plus-Preview | `qwen` | 思考、视觉、WebDev、幻灯片 | 邀请/预览系列，支持视觉入口，适合试验性接入。 |
| `qwen3.5-plus` | Qwen3.5-Plus | `qwen3.5` | 思考、搜索、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | 通用模型，能力覆盖完整，适合作为备用选择。 |
| `qwen3.5-omni-plus` | Qwen3.5-Omni-Plus | `qwen3.5` | 视觉、图片生成 | Omni 多模态模型，偏视觉和图片生成。 |
| `qwen3.6-35b-a3b` | Qwen3.6-35B-A3B | `qwen3.6` | 思考、搜索、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | MoE 通用模型，能力覆盖完整。 |
| `qwen3.5-flash` | Qwen3.5-Flash | `qwen3.5` | 思考、搜索、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | 快速响应模型；轻量任务、低延迟任务推荐。 |
| `qwen3.5-max-2026-03-08` | Qwen3.5-Max-Preview | `qwen3.5` | 思考、WebDev | 高质量文本/建站预览模型。 |
| `qwen3.6-plus-preview` | Qwen3.6-Plus-Preview | `qwen3.6` | 思考、WebDev | Plus 预览模型，适合文本与 WebDev 试验。 |
| `qwen3.5-397b-a17b` | Qwen3.5-397B-A17B | `qwen3.5` | 思考、搜索、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | 大参数 MoE 模型，适合复杂通用任务。 |
| `qwen3.5-122b-a10b` | Qwen3.5-122B-A10B | `qwen3.5` | 思考、搜索、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | 大参数 MoE 模型，适合复杂通用任务。 |
| `qwen3.5-omni-flash` | Qwen3.5-Omni-Flash | `qwen3.5` | 视觉、图片生成 | 快速 Omni 模型，偏视觉和图片生成。 |
| `qwen3.5-27b` | Qwen3.5-27B | `qwen3.5` | 思考、搜索、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | 中型通用模型，能力覆盖完整。 |
| `qwen3.5-35b-a3b` | Qwen3.5-35B-A3B | `qwen3.5` | 思考、搜索、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | MoE 通用模型，能力覆盖完整。 |
| `qwen3-max-2026-01-23` | Qwen3-Max | `qwen3` | 思考、搜索、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | Max 系列高质量模型，能力覆盖完整。 |
| `qwen-plus-2025-07-28` | Qwen3-235B-A22B-2507 | `qwen` | 思考、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | 旧版 plus 命名兼容模型，适合存量接入。 |
| `qwen3-coder-plus` | Qwen3-Coder | `qwen3` | 视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | 代码任务推荐；也支持多种生成模式。 |
| `qwen3-vl-plus` | Qwen3-VL-235B-A22B | `qwen3` | 思考、视觉、深度研究、图片生成、视频生成、WebDev、幻灯片 | 视觉语言模型推荐；适合视觉相关文本理解。 |
| `qwen3-omni-flash-2025-12-01` | Qwen3-Omni-Flash | `qwen3` | 思考、视觉、深度研究、图片生成、视频生成、WebDev | 快速 Omni 模型，适合多模态轻量任务。 |

### 2.3 模式后缀规则

以下后缀可作为模型 ID 的一部分传入接口。例如基础模型为 `qwen3.7-plus`，图片模式可传 `qwen3.7-plus-image`。

| 后缀 | mode | chat_type | 特点 |
|---|---|---|---|
| 无后缀 | `chat` | `t2t` | 普通文本对话。 |
| `-thinking` | `thinking` | `t2t` | 强制开启思考模式；即使请求中 `enable_thinking=false` 也会被覆盖。 |
| `-search` | `search` | `t2t` | 启用搜索；当前模型列表构建逻辑会为基础模型生成该变体。 |
| `-deep-research` | `deep_research` | `deep_research` | 深度研究模式，默认启用搜索。 |
| `-image` | `image` | `t2i` / `image_gen` | 图片生成模式；图片接口会解析到基础模型调用。 |
| `-t2i` | `image` | `t2i` | 图片生成兼容后缀。 |
| `-video` | `video` | `t2v` | 视频生成模式；视频接口会解析到基础模型调用。 |
| `-t2v` | `video` | `t2v` | 视频生成兼容后缀。 |
| `-webdev` | `webdev` | `web_dev` | Web/站点构建模式。 |
| `-web-dev` | `webdev` | `web_dev` | Web/站点构建兼容后缀。 |
| `-slides` | `slides` | `slides` | PPT/幻灯片模式。 |

### 2.4 当前快照中的图片模型

当前快照中 `mode=image` 的模型包括：

`qwen3.7-max-image`、`qwen3.7-plus-image`、`qwen3.6-plus-image`、`qwen3-max-2026-01-23-image`、`qwen3.6-35b-a3b-image`、`qwen3.5-plus-image`、`qwen3.6-27b-image`、`qwen3.5-omni-plus-image`、`qwen3.5-flash-image`、`qwen3.5-397b-a17b-image`、`qwen3.5-122b-a10b-image`、`qwen3.5-omni-flash-image`、`qwen3.5-27b-image`、`qwen3.5-35b-a3b-image`、`qwen-plus-2025-07-28-image`、`qwen3-coder-plus-image`、`qwen3-vl-plus-image`、`qwen3-omni-flash-2025-12-01-image`

图片生成按质量上限展示时首选：`qwen3.7-max-image`、`qwen3.7-plus-image`、`qwen3.6-plus-image`。如果传入 `qwen-image`、`qwen-image-plus`、`qwen-image-turbo`、`dall-e-2`、`dall-e-3`，图片接口会映射到 `qwen3.6-plus`。接口层未传 `model` 时仍使用后端兼容默认 `qwen3.6-plus`；WebUI 为了更好的默认体验，会默认选中 `qwen3.7-plus-image`。

### 2.5 当前快照中的视频模型

当前快照中 `mode=video` 的模型包括（来自 `/v1/models`；WebUI 会额外补充 `qwen-i2v` 兼容别名）：

`qwen3.7-max-video`、`qwen3.7-plus-video`、`qwen3.6-plus-video`、`qwen3-max-2026-01-23-video`、`qwen3.6-35b-a3b-video`、`qwen3.5-plus-video`、`qwen3.6-27b-video`、`qwen3.5-flash-video`、`qwen3.5-397b-a17b-video`、`qwen3.5-122b-a10b-video`、`qwen3.5-27b-video`、`qwen3.5-35b-a3b-video`、`qwen-plus-2025-07-28-video`、`qwen3-coder-plus-video`、`qwen3-vl-plus-video`、`qwen3-omni-flash-2025-12-01-video`

视频生成按质量上限展示时首选：`qwen3.7-max-video`、`qwen3.7-plus-video`、`qwen-i2v`、`qwen3.6-plus-video`。如果传入 `qwen-video`、`qwen-video-plus`、`qwen-video-turbo`、`qwen3.6-plus-video`，文生视频接口会映射到 `qwen3.6-plus` 或对应基础模型。接口层未传 `model` 时仍使用后端兼容默认 `qwen3.6-plus`；WebUI 为了更好的默认体验，会默认选中 `qwen3.7-plus-video`。传入首帧图片进入图生视频时，接口切换为 `i2v`；当前实测可用的 I2V 模型包括：`qwen-i2v`、`qwen3.7-max-video`、`qwen3.7-plus-video`、`qwen3.6-plus-video`、`qwen3-max-2026-01-23-video`、`qwen3.6-35b-a3b-video`、`qwen3.5-plus-video`、`qwen3.6-27b-video`、`qwen3.5-flash-video`、`qwen3.5-397b-a17b-video`、`qwen3.5-122b-a10b-video`、`qwen3.5-27b-video`、`qwen3.5-35b-a3b-video`、`qwen-plus-2025-07-28-video`、`qwen3-coder-plus-video`、`qwen3-vl-plus-video`、`qwen3-omni-flash-2025-12-01-video`。其中 `qwen-i2v` 是兼容别名，默认解析为 `qwen3.7-plus`；其它 `*-video` 模型会自动剥离 `-video` 后缀后调用对应基础模型。

注意：WebUI 的图片/视频模型下拉会按质量上限排序展示；`/v1/models` 返回运行时模型列表，不保证与 WebUI 展示顺序一致，第三方客户端如需同样体验应按本节推荐顺序自行排序。

## 3. 模型查询接口

### 3.1 获取模型列表

```http
GET /v1/models
```

请求头：

| 参数 | 必填 | 示例值 | 含义 |
|---|---:|---|---|
| `Authorization` | 是 | `Bearer YOUR_API_KEY` | 客户端 API Key。 |

curl 示例：

```bash
curl "https://qwen2api.codeai.de5.net/v1/models" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

返回示例：

```json
{
  "object": "list",
  "data": [
    {
      "id": "qwen3.7-plus",
      "object": "model",
      "created": 1700000000,
      "owned_by": "qwen",
      "capabilities": {
        "thinking": true,
        "search": true,
        "vision": true,
        "deep_research": true,
        "image_gen": true,
        "video_gen": true,
        "web_dev": true,
        "slides": true
      },
      "base_model": "qwen3.7-plus",
      "mode": "chat",
      "display_name": "Qwen3.7-Plus",
      "family": "qwen3.7"
    }
  ]
}
```

返回字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `object` | string | 固定为 `list`。 |
| `data` | array | 模型对象数组。 |
| `data[].id` | string | 客户端调用时传入的模型 ID。 |
| `data[].object` | string | 固定为 `model`。 |
| `data[].created` | integer | 创建时间戳；兼容字段，不一定等同上游真实发布时间。 |
| `data[].owned_by` | string | 模型归属方。 |
| `data[].capabilities` | object | 能力标记对象。 |
| `data[].capabilities.thinking` | boolean | 是否支持思考模式。 |
| `data[].capabilities.search` | boolean | 是否支持搜索能力。 |
| `data[].capabilities.vision` | boolean | 是否支持视觉输入或视觉相关能力。 |
| `data[].capabilities.deep_research` | boolean | 是否支持深度研究模式。 |
| `data[].capabilities.image_gen` | boolean | 是否支持图片生成。 |
| `data[].capabilities.video_gen` | boolean | 是否支持视频生成。 |
| `data[].capabilities.web_dev` | boolean | 是否支持 Web/建站模式。 |
| `data[].capabilities.slides` | boolean | 是否支持幻灯片模式。 |
| `data[].base_model` | string | 基础模型 ID；后缀模型会指向其基础模型。 |
| `data[].mode` | string | 常见值：`chat`、`thinking`、`search`、`deep_research`、`image`、`video`、`webdev`、`slides`。 |
| `data[].display_name` | string | 展示名称。 |
| `data[].family` | string | 模型家族。 |

### 3.2 获取单个模型信息

```http
GET /v1/models/{model_id}
```

示例：

```bash
curl "https://qwen2api.codeai.de5.net/v1/models/qwen3.7-plus-image" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

返回示例：

```json
{
  "id": "qwen3.7-plus-image",
  "object": "model",
  "created": 1700000000,
  "owned_by": "qwen2api",
  "capabilities": {"image_gen": true},
  "base_model": "qwen3.7-plus",
  "mode": "image",
  "display_name": "qwen3.7-plus-image",
  "family": "qwen3.7",
  "resolved_model": "qwen3.7-plus"
}
```

## 4. 文本对话接口

### 4.1 接口路径

```http
POST /v1/chat/completions
POST /chat/completions
```

### 4.2 非流式对话示例

```bash
curl "https://qwen2api.codeai.de5.net/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "qwen3.7-plus",
    "messages": [
      {"role": "system", "content": "你是一个专业、简洁的助手。"},
      {"role": "user", "content": "用三句话介绍 qwen2api 的作用。"}
    ],
    "stream": false,
    "enable_thinking": false,
    "temperature": 0.7,
    "max_tokens": 1024
  }'
```

### 4.3 流式对话示例

```bash
curl -N "https://qwen2api.codeai.de5.net/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "qwen3.7-plus-thinking",
    "messages": [
      {"role": "user", "content": "写一个 Python 快速排序示例。"}
    ],
    "stream": true
  }'
```

### 4.4 请求参数

| 参数 | 类型 | 必填 | 默认值 | 可选值/示例值 | 含义 |
|---|---|---:|---|---|---|
| `model` | string | 否 | `gpt-3.5-turbo`，会解析为 `qwen3.5-flash` | `qwen3.7-plus`、`qwen3.5-flash`、`qwen3-coder-plus` | 模型 ID，可使用基础模型或模式后缀模型。 |
| `messages` | array | 是 | - | OpenAI Chat Messages | 对话消息数组。 |
| `messages[].role` | string | 是 | - | `system`、`user`、`assistant`、`tool` | 消息角色。 |
| `messages[].content` | string/array | 是 | - | `"你好"` 或内容块数组 | 消息内容。纯文本建议使用 string。 |
| `stream` | boolean | 否 | `false` | `true`、`false` | 是否使用 SSE 流式响应。 |
| `enable_thinking` | boolean/string/number | 否 | 未显式指定 | `true`、`false`、`"thinking"`、`"fast"`、`1`、`0` | 控制思考模式。`*-thinking` 后缀会强制开启。 |
| `thinking` | boolean/object | 否 | - | `true`、`false`、`{"enabled": true}` | 思考模式兼容字段。 |
| `thinking_mode` | string | 否 | - | `auto`、`thinking`、`fast`、`off` | 思考模式兼容字段。 |
| `enable_search` | boolean/string/number | 否 | `false` | `true`、`false` | 是否启用搜索。`*-search` 和 `*-deep-research` 会自动启用。 |
| `temperature` | number | 否 | 上游决定 | `0.7` | 采样温度，具体效果受上游影响。 |
| `max_tokens` | integer | 否 | 上游决定 | `1024` | 期望最大输出长度，具体截断受上游影响。 |
| `tools` | array | 否 | `[]` | OpenAI tools 格式 | 工具定义；qwen2api 会尝试转换为兼容工具调用。 |
| `tool_choice` | string/object | 否 | - | `auto`、`none`、指定工具 | 工具选择策略。 |
| `session_key` | string | 否 | 自动派生 | `user-123-session` | 会话亲和键，用于上下文复用。 |

### 4.5 非流式返回

返回示例：

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1780000000,
  "model": "qwen3.7-plus",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "qwen2api 是一个自托管 API 网关。它将 Qwen Web 能力转换为 OpenAI 兼容接口。客户端可以通过统一 Base URL 调用文本、图片和视频能力。"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 120,
    "completion_tokens": 80,
    "total_tokens": 200
  }
}
```

返回字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | string | 本次 completion ID，格式类似 `chatcmpl-*`。 |
| `object` | string | 固定为 `chat.completion`。 |
| `created` | integer | 服务端创建响应的 Unix 时间戳。 |
| `model` | string | 本次请求传入的模型 ID。 |
| `choices` | array | 候选结果数组，当前通常返回一个结果。 |
| `choices[].index` | integer | 候选结果序号。 |
| `choices[].message.role` | string | 固定为 `assistant`。 |
| `choices[].message.content` | string/null | 可见文本内容；工具调用场景可能为 `null`。 |
| `choices[].message.reasoning_content` | string | 思考内容；仅当上游返回 reasoning 且服务未过滤时出现。 |
| `choices[].message.tool_calls` | array | 工具调用数组；仅工具调用场景出现。 |
| `choices[].finish_reason` | string | 常见值：`stop`、`tool_calls`。 |
| `usage.prompt_tokens` | integer | 输入 token 估算值；当前实现按内部长度估算，不等同官方精确 token。 |
| `usage.completion_tokens` | integer | 输出 token 估算值。 |
| `usage.total_tokens` | integer | 输入与输出估算值之和。 |

### 4.6 流式返回

流式响应类型为：

```http
Content-Type: text/event-stream
```

普通文本分片示例：

```text
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1780000000,"model":"qwen3.7-plus","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1780000000,"model":"qwen3.7-plus","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}

data: [DONE]
```

思考内容分片可能出现在：

```json
{"choices":[{"delta":{"reasoning_content":"这里是思考内容"}}]}
```

工具调用分片可能出现在：

```json
{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_xxx","type":"function","function":{"name":"tool_name","arguments":"{}"}}]}}]}
```

## 5. 图片生成与下载

### 5.1 接口路径

```http
POST /v1/images/generations
POST /images/generations
```

### 5.2 图片生成 curl 示例

```bash
curl "https://qwen2api.codeai.de5.net/v1/images/generations" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "qwen3.7-plus-image",
    "prompt": "一只赛博朋克风格的猫，霓虹灯背景，超写实，电影感光影",
    "n": 1,
    "size": "1328x1328",
    "response_format": "url"
  }'
```

### 5.3 请求参数

| 参数 | 类型 | 必填 | 默认值 | 可选值/示例值 | 含义 |
|---|---|---:|---|---|---|
| `prompt` | string | 是 | - | 非空字符串 | 图片生成提示词。建议包含主体、风格、场景、构图和质量要求。 |
| `model` | string | 否 | `qwen3.6-plus` | `qwen3.7-max-image`、`qwen3.7-plus-image`、`qwen3.6-plus-image`、`qwen-image`、`dall-e-3` | 图片模型或兼容别名。接口会解析后调用基础模型。 |
| `n` | integer | 否 | `1` | `1~4` | 期望返回图片数量。实现会限制在 1~4。 |
| `size` | string | 否 | `1328x1328` | 见尺寸表 | 目标尺寸。可直接传尺寸或比例。 |
| `ratio` | string | 否 | `1:1` | 见尺寸表 | 目标宽高比；`size`、`ratio`、`aspect_ratio` 三者任选其一。 |
| `aspect_ratio` | string | 否 | `1:1` | 见尺寸表 | `ratio` 的兼容别名。 |
| `response_format` | string | 否 | `url` | `url` | 返回格式。当前实现始终返回 URL，不返回 base64。 |

支持尺寸和比例：

| `size` | `ratio` / `aspect_ratio` | 说明 |
|---|---|---|
| `1328x1328` | `1:1` | 方图，默认值。 |
| `1664x928` | `16:9` | 横向宽屏。 |
| `928x1664` | `9:16` | 竖屏。 |
| `1472x1140` | `4:3` | 横向标准比例。 |
| `1140x1472` | `3:4` | 竖向标准比例。 |

非法尺寸或比例会回退到：`1328x1328 / 1:1`。

### 5.4 图片生成返回

返回示例：

```json
{
  "created": 1780000000,
  "data": [
    {
      "url": "https://example.com/generated/qwen-image.png",
      "revised_prompt": "一只赛博朋克风格的猫，霓虹灯背景，超写实，电影感光影",
      "size": "1328x1328",
      "ratio": "1:1",
      "width": 1328,
      "height": 1328
    }
  ]
}
```

返回字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `created` | integer | 响应创建时间戳。 |
| `data` | array | 图片结果数组。 |
| `data[].url` | string | 图片 URL，通常是带签名的临时 CDN 链接。 |
| `data[].revised_prompt` | string | 本次生成使用的提示词，当前通常等于请求 `prompt`。 |
| `data[].size` | string | 实际请求尺寸。 |
| `data[].ratio` | string | 实际请求宽高比。 |
| `data[].width` | integer | 实际请求宽度。 |
| `data[].height` | integer | 实际请求高度。 |

### 5.5 下载图片

图片 URL 位于：

```text
data[0].url
```

curl 下载：

```bash
IMAGE_URL="从 data[0].url 复制出来的图片链接"
curl -L "$IMAGE_URL" -o "qwen-image.png"
```

Python 下载：

```python
import requests

url = result["data"][0]["url"]
resp = requests.get(url, timeout=60)
resp.raise_for_status()
with open("qwen-image.png", "wb") as f:
    f.write(resp.content)
```

前端下载：

```javascript
const imageUrl = result.data[0].url;
const response = await fetch(imageUrl);
const blob = await response.blob();
const objectUrl = URL.createObjectURL(blob);
const a = document.createElement("a");
a.href = objectUrl;
a.download = "qwen-image.png";
a.click();
URL.revokeObjectURL(objectUrl);
```

注意：图片 URL 通常是临时签名链接，建议生成后立即下载并转存到自己的对象存储或 CDN。

## 6. 视频生成与下载

### 6.1 接口路径

```http
POST /v1/videos/generations
POST /videos/generations
```

### 6.2 视频生成 curl 示例

```bash
curl "https://qwen2api.codeai.de5.net/v1/videos/generations" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "qwen3.7-plus-video",
    "prompt": "雨夜霓虹街头，一只黑猫慢慢穿过水洼，电影感镜头，真实光影",
    "n": 1,
    "size": "1664x928",
    "ratio": "16:9",
    "duration": 3,
    "response_format": "url"
  }'
```

公网域名如果托管在 Cloudflare 橙云后，推荐显式使用异步任务，避免视频生成长请求触发 Cloudflare 代理超时：

```bash
TASK_ID=$(curl "https://qwen2api.codeai.de5.net/v1/videos/generations" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "qwen3.7-plus-video",
    "prompt": "雨夜霓虹街头，一只黑猫慢慢穿过水洼，电影感镜头，真实光影",
    "n": 1,
    "ratio": "16:9",
    "duration": 5,
    "async": true
  }' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl "https://qwen2api.codeai.de5.net/v1/videos/tasks/$TASK_ID" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### 6.3 请求参数

| 参数 | 类型 | 必填 | 默认值 | 可选值/示例值 | 含义 |
|---|---|---:|---|---|---|
| `prompt` | string | 是 | - | 非空字符串 | 视频生成提示词。建议包含主体、动作、镜头、场景、风格。 |
| `model` | string | 否 | `qwen3.6-plus` | `qwen3.7-max-video`、`qwen3.7-plus-video`、`qwen-i2v`、`qwen3.6-plus-video`、`qwen-video` | 视频模型或兼容别名。接口会解析后调用基础模型。 |
| `n` | integer | 否 | `1` | `1~2` | 期望返回视频数量。实现会限制在 1~2。 |
| `size` | string | 否 | `1328x1328` | 见尺寸表 | 参考画面尺寸。可传尺寸或比例。 |
| `ratio` | string | 否 | `1:1` | 见尺寸表 | 目标宽高比。 |
| `aspect_ratio` | string | 否 | `1:1` | 见尺寸表 | `ratio` 的兼容别名。 |
| `duration` | integer/string | 否 | `5` | `1~10` | 视频时长，单位秒。实现会限制在 1~10。 |
| `response_format` | string | 否 | `url` | `url` | 返回格式。当前实现返回视频 URL。 |
| `async` | boolean/string | 否 | `false` | `true`、`false`、`"true"`、`"1"` | 是否创建后台视频任务。`false` 保持同步响应；`true` 立即返回任务 ID，客户端轮询任务状态。 |
| `file_id` | string | 否 | - | `/v1/files` 返回的文件 ID | 图生视频首帧图片。推荐先调用 `/v1/files` 上传图片，再传该 ID。 |
| `image_url` | string/object | 否 | - | `https://example.com/first.png`、`data:image/png;base64,...`、`{"url":"..."}` | 图生视频首帧图片 URL。远程 URL 会由服务端下载后再上传到 Qwen Web OSS。 |
| `first_frame` | string/object | 否 | - | `"data:image/png;base64,..."`、`{"file_id":"..."}`、`{"url":"..."}` | 首帧图片兼容字段。 |

`file_id`、`image_url`、`first_frame` 三类首帧来源最多只能传一个。未传首帧时接口保持文生视频 `t2v`；传入首帧时接口切换到 Qwen Web 已实测可用的图生视频 `i2v`。`qwen-i2v` 会解析为默认 I2V 模型 `qwen3.7-plus`；实测通过的 `*-video` 模型也可用于 I2V，接口会自动剥离 `-video` 后缀后调用对应基础模型。首帧图片要求：

- MIME 类型必须为 `image/*`。
- 文件不能为空。
- 最大 20MB。
- 远程 URL 仅允许 `http/https`，且服务端会拒绝 localhost、私网、链路本地和保留地址，降低 SSRF 风险。

视频接口复用图片尺寸归一化逻辑，支持：

| `size` | `ratio` / `aspect_ratio` | 说明 |
|---|---|---|
| `1328x1328` | `1:1` | 方形视频，默认值。 |
| `1664x928` | `16:9` | 横向视频，常规视频推荐。 |
| `928x1664` | `9:16` | 竖屏短视频。 |
| `1472x1140` | `4:3` | 横向标准比例。 |
| `1140x1472` | `3:4` | 竖向标准比例。 |

非法尺寸或比例会回退到：`1328x1328 / 1:1`。

### 6.4 图生视频首帧示例

推荐方式：先上传图片得到 `file_id`，再生成视频。

```bash
FILE_ID=$(curl "https://qwen2api.codeai.de5.net/v1/files" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -F "file=@/path/to/first-frame.png" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl "https://qwen2api.codeai.de5.net/v1/videos/generations" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d "{
    \"model\": \"qwen-i2v\",
    \"prompt\": \"让首帧中的主体轻微运动，镜头缓慢推进，电影感光影\",
    \"file_id\": \"$FILE_ID\",
    \"n\": 1,
    \"ratio\": \"16:9\",
    \"duration\": 5,
    \"response_format\": \"url\"
  }"
```

也可以直接传 data URI：

```json
{
  "model": "qwen-image-to-video",
  "prompt": "让首帧中的主体轻微运动，镜头缓慢推进",
  "first_frame": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg...",
  "ratio": "16:9",
  "duration": 5,
  "response_format": "url"
}
```

或者传远程图片 URL：

```json
{
  "model": "qwen-video-i2v",
  "prompt": "保持首帧构图，让画面有轻微动态和缓慢推镜",
  "image_url": "https://example.com/first-frame.png",
  "ratio": "16:9",
  "duration": 5,
  "response_format": "url"
}
```

当前 Qwen Web token 链路实测提交生成时使用 `chat_type=i2v` / `sub_chat_type=i2v`，图片会先上传到 Qwen Web OSS，然后作为 `files[]` 中的视觉图片引用传给上游。

### 6.5 视频生成返回

返回示例：

```json
{
  "created": 1780000000,
  "data": [
    {
      "url": "https://example.com/generated/qwen-video.mp4",
      "revised_prompt": "雨夜霓虹街头，一只黑猫慢慢穿过水洼，电影感镜头，真实光影",
      "size": "1664x928",
      "ratio": "16:9",
      "width": 1664,
      "height": 928,
      "duration": 3
    }
  ]
}
```

返回字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `created` | integer | 响应创建时间戳。 |
| `data` | array | 视频结果数组。 |
| `data[].url` | string | 视频 URL，通常指向 MP4 文件。 |
| `data[].revised_prompt` | string | 本次生成使用的提示词，当前通常等于请求 `prompt`。 |
| `data[].size` | string | 实际请求参考尺寸。 |
| `data[].ratio` | string | 实际请求宽高比。 |
| `data[].width` | integer | 实际请求宽度。 |
| `data[].height` | integer | 实际请求高度。 |
| `data[].duration` | integer | 实际请求的视频时长，单位秒。 |

### 6.6 异步任务轮询

`async: true` 时创建接口返回 `200 OK` 和任务对象：

```json
{
  "id": "video_task_xxx",
  "object": "video.generation.task",
  "status": "queued",
  "created": 1780000000,
  "updated": 1780000000,
  "model": "qwen3.7-plus",
  "mode": "t2v",
  "poll_url": "/v1/videos/tasks/video_task_xxx"
}
```

轮询接口：

```http
GET /v1/videos/tasks/{task_id}
GET /videos/tasks/{task_id}
```

状态枚举：`queued`、`running`、`succeeded`、`failed`、`interrupted`、`expired`。完成后响应中包含与同步接口一致的 `data[]` 视频结果；失败时返回 `error.code` 和脱敏后的 `error.message`。任务仅创建它的 API Key 可查询，管理员 Key 可查询全部。

### 6.7 下载视频

视频 URL 位于：

```text
data[0].url
```

curl 下载：

```bash
VIDEO_URL="从 data[0].url 复制出来的视频链接"
curl -L "$VIDEO_URL" -o "qwen-video.mp4"
```

Python 下载：

```python
import requests

url = result["data"][0]["url"]
resp = requests.get(url, timeout=180)
resp.raise_for_status()
with open("qwen-video.mp4", "wb") as f:
    f.write(resp.content)
```

前端下载：

```javascript
const videoUrl = result.data[0].url;
const response = await fetch(videoUrl);
const blob = await response.blob();
const objectUrl = URL.createObjectURL(blob);
const a = document.createElement("a");
a.href = objectUrl;
a.download = "qwen-video.mp4";
a.click();
URL.revokeObjectURL(objectUrl);
```

注意：公网域名走 Cloudflare 橙云时，视频生成推荐传 `async: true` 并轮询 `/v1/videos/tasks/{task_id}`；同步模式仍可用于内网或调试场景，客户端需设置 360 秒以上超时。返回视频 URL 通常是临时签名链接，建议立即下载并转存。

## 7. Python requests 完整示例

### 7.1 对话

```python
import requests

BASE_URL = "https://qwen2api.codeai.de5.net"
API_KEY = "YOUR_API_KEY"

resp = requests.post(
    f"{BASE_URL}/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    json={
        "model": "qwen3.7-plus",
        "messages": [{"role": "user", "content": "用一句话介绍 qwen2api。"}],
        "stream": False,
        "enable_thinking": False,
    },
    timeout=120,
)
resp.raise_for_status()
result = resp.json()
print(result["choices"][0]["message"]["content"])
```

### 7.2 生成并下载图片

```python
import requests

BASE_URL = "https://qwen2api.codeai.de5.net"
API_KEY = "YOUR_API_KEY"

resp = requests.post(
    f"{BASE_URL}/v1/images/generations",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    json={
        "model": "qwen3.7-plus-image",
        "prompt": "极简风格的山水海报，留白，高级灰配色",
        "n": 1,
        "size": "1328x1328",
        "response_format": "url",
    },
    timeout=180,
)
resp.raise_for_status()
result = resp.json()
image_url = result["data"][0]["url"]

image_resp = requests.get(image_url, timeout=60)
image_resp.raise_for_status()
with open("qwen-image.png", "wb") as f:
    f.write(image_resp.content)
```

### 7.3 生成并下载视频

```python
import requests

BASE_URL = "https://qwen2api.codeai.de5.net"
API_KEY = "YOUR_API_KEY"

resp = requests.post(
    f"{BASE_URL}/v1/videos/generations",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    json={
        "model": "qwen3.7-plus-video",
        "prompt": "一颗红色小球在白色桌面上缓慢滚动，干净棚拍风格",
        "n": 1,
        "size": "1664x928",
        "ratio": "16:9",
        "duration": 3,
        "response_format": "url",
    },
    timeout=360,
)
resp.raise_for_status()
result = resp.json()
video_url = result["data"][0]["url"]

video_resp = requests.get(video_url, timeout=180)
video_resp.raise_for_status()
with open("qwen-video.mp4", "wb") as f:
    f.write(video_resp.content)
```

公网 Cloudflare 橙云推荐异步写法：

```python
import time
import requests

BASE_URL = "https://qwen2api.codeai.de5.net"
API_KEY = "YOUR_API_KEY"
headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

create_resp = requests.post(
    f"{BASE_URL}/v1/videos/generations",
    headers=headers,
    json={
        "model": "qwen3.7-plus-video",
        "prompt": "一颗红色小球在白色桌面上缓慢滚动，干净棚拍风格",
        "ratio": "16:9",
        "duration": 5,
        "async": True,
    },
    timeout=30,
)
create_resp.raise_for_status()
task = create_resp.json()

while task["status"] in {"queued", "running"}:
    time.sleep(8)
    poll_resp = requests.get(
        f"{BASE_URL}/v1/videos/tasks/{task['id']}",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=30,
    )
    poll_resp.raise_for_status()
    task = poll_resp.json()

if task["status"] != "succeeded":
    raise RuntimeError(task.get("error", task))

video_url = task["data"][0]["url"]
print(video_url)
```

### 7.4 上传首帧并生成图生视频

```python
import requests

BASE_URL = "https://qwen2api.codeai.de5.net"
API_KEY = "YOUR_API_KEY"

headers = {"Authorization": f"Bearer {API_KEY}"}

with open("/path/to/first-frame.png", "rb") as f:
    upload_resp = requests.post(
        f"{BASE_URL}/v1/files",
        headers=headers,
        files={"file": ("first-frame.png", f, "image/png")},
        timeout=60,
    )
upload_resp.raise_for_status()
file_id = upload_resp.json()["id"]

resp = requests.post(
    f"{BASE_URL}/v1/videos/generations",
    headers={**headers, "Content-Type": "application/json"},
    json={
        "model": "qwen-i2v",
        "prompt": "让首帧中的主体轻微运动，镜头缓慢推进，电影感光影",
        "file_id": file_id,
        "n": 1,
        "ratio": "16:9",
        "duration": 5,
        "response_format": "url",
    },
    timeout=360,
)
resp.raise_for_status()
video_url = resp.json()["data"][0]["url"]

video_resp = requests.get(video_url, timeout=180)
video_resp.raise_for_status()
with open("qwen-i2v.mp4", "wb") as f:
    f.write(video_resp.content)
```

## 8. JavaScript fetch 完整示例

### 8.1 对话

```javascript
const BASE_URL = "https://qwen2api.codeai.de5.net";
const API_KEY = "YOUR_API_KEY";

const response = await fetch(`${BASE_URL}/v1/chat/completions`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${API_KEY}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    model: "qwen3.7-plus",
    messages: [{ role: "user", content: "用一句话介绍 qwen2api。" }],
    stream: false,
    enable_thinking: false,
  }),
});

if (!response.ok) {
  throw new Error(await response.text());
}

const result = await response.json();
console.log(result.choices[0].message.content);
```

### 8.2 生成图片并获取 URL

```javascript
const response = await fetch("https://qwen2api.codeai.de5.net/v1/images/generations", {
  method: "POST",
  headers: {
    Authorization: "Bearer YOUR_API_KEY",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    model: "qwen3.7-plus-image",
    prompt: "一张未来感城市夜景海报，霓虹灯，高细节",
    n: 1,
    size: "1328x1328",
    response_format: "url",
  }),
});

if (!response.ok) {
  throw new Error(await response.text());
}

const result = await response.json();
const imageUrl = result.data[0].url;
console.log(imageUrl);
```

### 8.3 生成视频并获取 URL

```javascript
const controller = new AbortController();
const timeout = setTimeout(() => controller.abort(), 360_000);

try {
  const response = await fetch("https://qwen2api.codeai.de5.net/v1/videos/generations", {
    method: "POST",
    headers: {
      Authorization: "Bearer YOUR_API_KEY",
      "Content-Type": "application/json",
    },
    signal: controller.signal,
    body: JSON.stringify({
      model: "qwen3.7-plus-video",
      prompt: "雨夜霓虹街头，一只黑猫慢慢穿过水洼，电影感镜头",
      n: 1,
      size: "1664x928",
      ratio: "16:9",
      duration: 3,
      response_format: "url",
    }),
  });

  if (!response.ok) {
    throw new Error(await response.text());
  }

  const result = await response.json();
  const videoUrl = result.data[0].url;
  console.log(videoUrl);
} finally {
  clearTimeout(timeout);
}
```

公网 Cloudflare 橙云推荐异步写法：

```javascript
async function createVideoAsync() {
  const createResponse = await fetch("https://qwen2api.codeai.de5.net/v1/videos/generations", {
    method: "POST",
    headers: {
      Authorization: "Bearer YOUR_API_KEY",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: "qwen3.7-plus-video",
      prompt: "雨夜霓虹街头，一只黑猫慢慢穿过水洼，电影感镜头",
      ratio: "16:9",
      duration: 5,
      async: true,
    }),
  });

  if (!createResponse.ok) throw new Error(await createResponse.text());
  let task = await createResponse.json();

  while (task.status === "queued" || task.status === "running") {
    await new Promise((resolve) => setTimeout(resolve, 8000));
    const pollResponse = await fetch(`https://qwen2api.codeai.de5.net/v1/videos/tasks/${task.id}`, {
      headers: { Authorization: "Bearer YOUR_API_KEY" },
    });
    if (!pollResponse.ok) throw new Error(await pollResponse.text());
    task = await pollResponse.json();
  }

  if (task.status !== "succeeded") {
    throw new Error(JSON.stringify(task.error || task));
  }
  return task.data[0].url;
}
```

### 8.4 上传首帧并生成图生视频

```javascript
const BASE_URL = "https://qwen2api.codeai.de5.net";
const API_KEY = "YOUR_API_KEY";

const formData = new FormData();
formData.append("file", firstFrameFile); // File 对象，例如 input.files[0]

const uploadResponse = await fetch(`${BASE_URL}/v1/files`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${API_KEY}`,
  },
  body: formData,
});

if (!uploadResponse.ok) {
  throw new Error(await uploadResponse.text());
}

const { id: fileId } = await uploadResponse.json();
const controller = new AbortController();
const timeout = setTimeout(() => controller.abort(), 360_000);

try {
  const response = await fetch(`${BASE_URL}/v1/videos/generations`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    signal: controller.signal,
    body: JSON.stringify({
      model: "qwen-i2v",
      prompt: "让首帧中的主体轻微运动，镜头缓慢推进，电影感光影",
      file_id: fileId,
      ratio: "16:9",
      duration: 5,
      response_format: "url",
    }),
  });

  if (!response.ok) {
    throw new Error(await response.text());
  }

  const result = await response.json();
  console.log(result.data[0].url);
} finally {
  clearTimeout(timeout);
}
```

## 9. 常见错误与处理建议

| HTTP 状态 | 错误示例 | 含义 | 处理建议 |
|---:|---|---|---|
| `400` | `Invalid JSON body` | 请求体不是合法 JSON。 | 检查 JSON 格式和 `Content-Type`。 |
| `400` | `prompt is required` | 图片或视频请求缺少 `prompt`。 | 传入非空提示词。 |
| `400` | `Only one of file_id, image_url, first_frame can be provided` | 图生视频首帧来源重复。 | 只保留一种首帧来源，推荐使用 `file_id`。 |
| `400` | `first frame must be an image MIME type ...` | 首帧不是图片 MIME。 | 上传 PNG/JPEG/WebP 等图片文件。 |
| `400` | `image_url resolves to a non-public address` | 远程首帧 URL 被 SSRF 防护拒绝。 | 使用公网可访问图片，或先上传到 `/v1/files` 后传 `file_id`。 |
| `401` | `Invalid API Key` | API Key 缺失或无效。 | 在 WebUI 创建客户端 Key，并使用 `Authorization: Bearer YOUR_API_KEY`。 |
| `402` | `Quota Exceeded` | 当前客户端 Key 额度不足。 | 调整额度或更换有效 Key。 |
| `500` | `Image generation produced no image URL` | 上游未返回可解析图片链接。 | 更换支持图片的模型，简化 prompt，稍后重试。 |
| `500` | `Video generation produced no video URL` | 上游未返回可解析视频链接。 | 更换支持视频的模型，降低复杂度，稍后重试。 |
| `500` | `No available accounts in pool (all busy or rate limited)` | 账号池忙碌或被限流。 | 降低并发、等待恢复或增加账号。 |
| `502` | `Qwen upstream error ...` | Qwen 上游明确返回失败。 | 查看服务端日志，确认账号、模型能力和上游限制。 |

## 10. 接入建议

### 10.1 推荐模型

| 场景 | 推荐模型 |
|---|---|
| 通用文本 | `qwen3.7-plus`、`qwen3.6-plus` |
| 高质量文本 | `qwen3.7-max`、`qwen3-max-2026-01-23` |
| 快速文本 | `qwen3.5-flash`、`qwen3.5-omni-flash` |
| 代码任务 | `qwen3-coder-plus` |
| 视觉相关文本 | `qwen3-vl-plus`、`qwen3-omni-flash-2025-12-01` |
| 图片生成 | `qwen3.7-max-image`、`qwen3.7-plus-image`、`qwen3.6-plus-image` |
| 视频生成 | `qwen3.7-max-video`、`qwen3.7-plus-video`、`qwen-i2v`、`qwen3.6-plus-video` |
| 首帧图生视频 | `qwen-i2v`、`qwen3.7-max-video`、`qwen3.7-plus-video`、`qwen3.6-plus-video`、`qwen3-max-2026-01-23-video`、`qwen3.6-35b-a3b-video`、`qwen3.5-plus-video`、`qwen3.6-27b-video`、`qwen3.5-flash-video`、`qwen3.5-397b-a17b-video`、`qwen3.5-122b-a10b-video`、`qwen3.5-27b-video`、`qwen3.5-35b-a3b-video`、`qwen-plus-2025-07-28-video`、`qwen3-coder-plus-video`、`qwen3-vl-plus-video`、`qwen3-omni-flash-2025-12-01-video` |

### 10.2 超时建议

| 接口 | 建议客户端超时 |
|---|---:|
| `/v1/models` | 30 秒 |
| `/v1/chat/completions` 非流式 | 120 秒 |
| `/v1/chat/completions` 流式 | 300 秒以上，按任务长度调整 |
| `/v1/images/generations` | 180 秒 |
| `/v1/videos/generations` | 360 秒以上 |
| 图片 URL 下载 | 60 秒 |
| 视频 URL 下载 | 180 秒以上 |

### 10.3 资源保存建议

- 文本结果读取 `choices[0].message.content`。
- 图片结果读取 `data[0].url` 后立即下载保存。
- 视频结果读取 `data[0].url` 后立即下载保存。
- 图片和视频 URL 不建议长期作为业务永久链接使用，应转存到业务自己的对象存储或 CDN。
- 调用前可先请求 `/v1/models`，按 `capabilities.image_gen`、`capabilities.video_gen`、`mode` 选择合适模型。
