import { API_BASE } from "./api"
import { getAuthHeader } from "./auth"

export type ModelCapability = {
  thinking?: boolean
  search?: boolean
  vision?: boolean
  deep_research?: boolean
  image_gen?: boolean
  video_gen?: boolean
  web_dev?: boolean
  slides?: boolean
}

export type ModelOption = {
  id: string
  base_model?: string
  family?: string
  mode?: string
  display_name?: string
  capabilities?: ModelCapability
}

export type ModelGroup = {
  family: string
  models: ModelOption[]
}

export const FALLBACK_CHAT_MODELS: ModelOption[] = [
  { id: "qwen3.6-plus", base_model: "qwen3.6-plus", family: "qwen3.6", mode: "chat", display_name: "qwen3.6-plus", capabilities: {} },
  { id: "qwen3.6-plus-thinking", base_model: "qwen3.6-plus", family: "qwen3.6", mode: "thinking", display_name: "qwen3.6-plus thinking", capabilities: { thinking: true } },
  { id: "qwen3.6-plus-search", base_model: "qwen3.6-plus", family: "qwen3.6", mode: "search", display_name: "qwen3.6-plus search", capabilities: { search: true } },
]

export const FALLBACK_IMAGE_MODELS: ModelOption[] = [
  { id: "qwen3.7-max-image", base_model: "qwen3.7-max", family: "qwen3.7", mode: "image", display_name: "qwen3.7-max image", capabilities: { image_gen: true } },
  { id: "qwen3.7-plus-image", base_model: "qwen3.7-plus", family: "qwen3.7", mode: "image", display_name: "qwen3.7-plus image", capabilities: { image_gen: true } },
  { id: "qwen3.6-plus-image", base_model: "qwen3.6-plus", family: "qwen3.6", mode: "image", display_name: "qwen3.6-plus image", capabilities: { image_gen: true } },
]

export const I2V_COMPAT_VIDEO_MODEL: ModelOption = {
  id: "qwen-i2v",
  base_model: "qwen3.7-plus",
  family: "i2v",
  mode: "video",
  display_name: "qwen-i2v",
  capabilities: { video_gen: true, vision: true },
}

export const FALLBACK_VIDEO_MODELS: ModelOption[] = [
  { id: "qwen3.7-max-video", base_model: "qwen3.7-max", family: "qwen3.7", mode: "video", display_name: "qwen3.7-max video", capabilities: { video_gen: true } },
  { id: "qwen3.7-plus-video", base_model: "qwen3.7-plus", family: "qwen3.7", mode: "video", display_name: "qwen3.7-plus video", capabilities: { video_gen: true } },
  I2V_COMPAT_VIDEO_MODEL,
  { id: "qwen3.6-plus-video", base_model: "qwen3.6-plus", family: "qwen3.6", mode: "video", display_name: "qwen3.6-plus video", capabilities: { video_gen: true } },
]

export const CAPABILITY_LABELS: Array<{ key: keyof ModelCapability; label: string }> = [
  { key: "thinking", label: "思考" },
  { key: "search", label: "搜索" },
  { key: "vision", label: "视觉" },
  { key: "deep_research", label: "研究" },
  { key: "image_gen", label: "图片" },
  { key: "video_gen", label: "视频" },
  { key: "web_dev", label: "建站" },
  { key: "slides", label: "PPT" },
]

const MODEL_MODE_SUFFIX_RE = /-(thinking|search|deep-research|deep_research|image|video|webdev|web-dev|slides|t2i|t2v)$/i
const TEXT_TEST_MODES = new Set(["chat", "thinking", "search", "deep_research"])
const GENERATION_MODES = new Set(["image", "video", "webdev", "slides"])
const IMAGE_MODEL_PRIORITY = [
  "qwen3.7-max-image",
  "qwen3.7-plus-image",
  "qwen3.6-plus-image",
  "qwen3-max-2026-01-23-image",
  "qwen3.6-35b-a3b-image",
  "qwen3.5-plus-image",
]
const VIDEO_MODEL_PRIORITY = [
  "qwen3.7-max-video",
  "qwen3.7-plus-video",
  "qwen-i2v",
  "qwen3.6-plus-video",
  "qwen3-max-2026-01-23-video",
  "qwen3.6-35b-a3b-video",
  "qwen3.5-plus-video",
]
const IMAGE_MODEL_PRIORITY_INDEX = new Map(IMAGE_MODEL_PRIORITY.map((id, index) => [id, index]))
const VIDEO_MODEL_PRIORITY_INDEX = new Map(VIDEO_MODEL_PRIORITY.map((id, index) => [id, index]))
const MODE_NAME_SUFFIX: Record<string, string> = {
  thinking: "thinking",
  search: "search",
  deep_research: "deep_research",
  image: "image",
  video: "video",
  webdev: "webdev",
  slides: "slides",
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {}
}

function modelMode(option: ModelOption): string {
  return option.mode || inferModeFromId(option.id)
}

function inferModeFromId(modelId: string): string {
  const id = modelId.toLowerCase()
  if (id.endsWith("-thinking")) return "thinking"
  if (id.endsWith("-search")) return "search"
  if (id.endsWith("-deep-research") || id.endsWith("-deep_research")) return "deep_research"
  if (id.endsWith("-image") || id.endsWith("-t2i")) return "image"
  if (id.endsWith("-video") || id.endsWith("-t2v")) return "video"
  if (id.endsWith("-webdev") || id.endsWith("-web-dev")) return "webdev"
  if (id.endsWith("-slides")) return "slides"
  return "chat"
}

function familyOf(option: ModelOption): string {
  if (option.family) return option.family
  const base = option.base_model || option.id.replace(MODEL_MODE_SUFFIX_RE, "")
  if (base.startsWith("qwen3.")) {
    const parts = base.split("-", 1)[0].split(".")
    if (parts.length >= 2) return parts.slice(0, 2).join(".")
  }
  return base.split("-", 1)[0] || "Qwen"
}

/**
 * 拼接可用于模型代际和规格判断的稳定文本。
 */
function modelSearchText(option: ModelOption): string {
  return [option.id, option.base_model, option.family, option.display_name]
    .filter(Boolean)
    .join(" ")
    .toLowerCase()
}

/**
 * 计算模型代际分数，分数越低优先级越高。
 */
function generationScore(text: string): number {
  if (text.includes("qwen3.7")) return 0
  if (text.includes("qwen3.6")) return 100
  if (text.includes("qwen3.5")) return 200
  if (text.includes("qwen3")) return 300
  if (text.includes("qwen")) return 400
  return 500
}

/**
 * 计算模型规格分数，分数越低质量上限越高。
 */
function tierScore(text: string): number {
  if (text.includes("max")) return 0
  if (text.includes("plus")) return 10
  if (text.includes("397b")) return 20
  if (text.includes("235b")) return 25
  if (text.includes("122b")) return 30
  if (text.includes("35b")) return 40
  if (text.includes("27b")) return 50
  if (text.includes("mini")) return 80
  if (text.includes("flash")) return 90
  return 70
}

/**
 * 按生成质量优先级排序模型，未显式配置的模型使用稳定兜底规则。
 */
function sortGenerationModels(options: ModelOption[], kind: "image" | "video"): ModelOption[] {
  const priorityIndex = kind === "image" ? IMAGE_MODEL_PRIORITY_INDEX : VIDEO_MODEL_PRIORITY_INDEX
  return [...options].sort((a, b) => {
    const explicitA = priorityIndex.get(a.id.toLowerCase())
    const explicitB = priorityIndex.get(b.id.toLowerCase())
    if (explicitA !== undefined || explicitB !== undefined) {
      return (explicitA ?? Number.MAX_SAFE_INTEGER) - (explicitB ?? Number.MAX_SAFE_INTEGER)
    }

    const textA = modelSearchText(a)
    const textB = modelSearchText(b)
    const scoreA = generationScore(textA) + tierScore(textA)
    const scoreB = generationScore(textB) + tierScore(textB)
    if (scoreA !== scoreB) return scoreA - scoreB
    return a.id.localeCompare(b.id)
  })
}

export function normalizeModelOption(value: unknown): ModelOption | null {
  if (typeof value === "string" && value) return { id: value, mode: inferModeFromId(value), capabilities: {} }
  const record = asRecord(value)
  const id = asText(record.id)
  if (!id) return null
  return {
    id,
    base_model: asText(record.base_model) || undefined,
    family: asText(record.family) || undefined,
    mode: asText(record.mode) || inferModeFromId(id),
    display_name: asText(record.display_name) || undefined,
    capabilities: asRecord(record.capabilities) as ModelCapability,
  }
}

export async function fetchModelOptions(): Promise<ModelOption[]> {
  const response = await fetch(`${API_BASE}/v1/models`, { headers: getAuthHeader() })
  if (!response.ok) return []
  const payload = await response.json()
  const rawItems = Array.isArray(payload?.data) ? payload.data : []
  return rawItems
    .map(normalizeModelOption)
    .filter((item: ModelOption | null): item is ModelOption => Boolean(item?.id))
}

export function isBaseModelOption(option: ModelOption): boolean {
  return option.base_model ? option.id === option.base_model : !MODEL_MODE_SUFFIX_RE.test(option.id)
}

export function isThinkingVariant(modelId: string): boolean {
  return /-thinking$/i.test(modelId)
}

export function capabilityBadges(option?: ModelOption): string[] {
  if (!option?.capabilities) return []
  return CAPABILITY_LABELS.filter(item => option.capabilities?.[item.key]).map(item => item.label)
}

export function filterTextTestModels(options: ModelOption[]): ModelOption[] {
  const filtered = options.filter(option => {
    const mode = modelMode(option)
    return TEXT_TEST_MODES.has(mode) && !GENERATION_MODES.has(mode)
  })
  const baseModels = filtered.filter(option => modelMode(option) === "chat")
  const existingIds = new Set(filtered.map(option => option.id))
  const searchVariants = baseModels
    .map(option => ({
      ...option,
      id: option.id.endsWith("-search") ? option.id : `${option.id}-search`,
      base_model: option.base_model || option.id,
      mode: "search",
      display_name: `${option.display_name || option.id} search`,
      capabilities: { search: true },
    }))
    .filter(option => !existingIds.has(option.id))
  const withSearch = [...filtered, ...searchVariants]
  return withSearch.length ? withSearch : FALLBACK_CHAT_MODELS
}

export function filterImageModels(options: ModelOption[]): ModelOption[] {
  const explicit = options.filter(option => modelMode(option) === "image")
  if (explicit.length) return sortGenerationModels(explicit, "image")
  const capable = options
    .filter(option => option.capabilities?.image_gen && !GENERATION_MODES.has(modelMode(option)))
    .map(option => ({
      ...option,
      id: option.id.endsWith("-image") ? option.id : `${option.id}-image`,
      base_model: option.base_model || option.id,
      mode: "image",
      display_name: `${option.display_name || option.id} image`,
      capabilities: { image_gen: true },
    }))
  return sortGenerationModels(capable.length ? capable : FALLBACK_IMAGE_MODELS, "image")
}

function withI2VCompatModel(options: ModelOption[]): ModelOption[] {
  if (options.some(option => option.id === I2V_COMPAT_VIDEO_MODEL.id)) return options
  return [...options, I2V_COMPAT_VIDEO_MODEL]
}

export function filterVideoModels(options: ModelOption[]): ModelOption[] {
  const explicit = options.filter(option => modelMode(option) === "video")
  if (explicit.length) return sortGenerationModels(withI2VCompatModel(explicit), "video")
  const capable = options
    .filter(option => option.capabilities?.video_gen && !GENERATION_MODES.has(modelMode(option)))
    .map(option => ({
      ...option,
      id: option.id.endsWith("-video") ? option.id : `${option.id}-video`,
      base_model: option.base_model || option.id,
      mode: "video",
      display_name: `${option.display_name || option.id} video`,
      capabilities: { video_gen: true },
    }))
  return sortGenerationModels(withI2VCompatModel(capable.length ? capable : FALLBACK_VIDEO_MODELS), "video")
}

export function chooseDefaultModel(options: ModelOption[], currentModel?: string, preferredId?: string): string {
  if (currentModel && options.some(option => option.id === currentModel)) return currentModel
  if (preferredId && options.some(option => option.id === preferredId)) return preferredId
  const base = options.find(isBaseModelOption)
  return base?.id || options[0]?.id || preferredId || "qwen3.6-plus"
}

/**
 * 按模型家族分组；保序模式使用连续分组，避免 optgroup 改变能力排序。
 */
export function groupModelOptions(options: ModelOption[], config: { preserveOrder?: boolean } = {}): ModelGroup[] {
  if (config.preserveOrder) {
    return options.reduce<ModelGroup[]>((groups, option) => {
      const family = familyOf(option)
      const lastGroup = groups[groups.length - 1]
      // 相邻同家族合并，跨家族不回填，保证下拉顺序与排序结果一致。
      if (lastGroup?.family === family) {
        lastGroup.models.push(option)
      } else {
        groups.push({ family, models: [option] })
      }
      return groups
    }, [])
  }

  const groups = new Map<string, ModelOption[]>()
  options.forEach(option => {
    const family = familyOf(option)
    groups.set(family, [...(groups.get(family) || []), option])
  })
  return Array.from(groups.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([family, models]) => ({
      family,
      models: [...models].sort((a, b) => a.id.localeCompare(b.id)),
    }))
}

export function formatModeLabel(mode?: string): string {
  switch (mode) {
    case "thinking": return "思考"
    case "search": return "搜索"
    case "deep_research": return "研究"
    case "image": return "图片"
    case "video": return "视频"
    case "webdev": return "建站"
    case "slides": return "PPT"
    default: return "对话"
  }
}

export function formatModelName(option: ModelOption): string {
  const mode = modelMode(option)
  const suffix = MODE_NAME_SUFFIX[mode]
  const rawName = option.display_name || option.id
  const name = suffix ? rawName.replace(new RegExp(`\\s+${suffix}$`, "i"), "") : rawName
  return name === option.id ? option.id : `${name} (${option.id})`
}

export function formatModelOptionLabel(option: ModelOption): string {
  return `${formatModelName(option)} · ${formatModeLabel(modelMode(option))}`
}
