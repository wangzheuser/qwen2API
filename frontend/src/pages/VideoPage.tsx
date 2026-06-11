import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from "react"
import { Download, ExternalLink, Film, History, Image as ImageIcon, RefreshCw, Trash2, UploadCloud, Video as VideoIcon, Wand2, X } from "lucide-react"
import { Button } from "../components/ui/button"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import {
  FALLBACK_VIDEO_MODELS,
  chooseDefaultModel,
  fetchModelOptions,
  filterVideoModels,
  formatModelOptionLabel,
  groupModelOptions,
  type ModelOption,
} from "../lib/models"

const ASPECT_RATIOS = [
  { label: "1:1", value: "1:1", w: 1328, h: 1328 },
  { label: "16:9", value: "16:9", w: 1664, h: 928 },
  { label: "9:16", value: "9:16", w: 928, h: 1664 },
  { label: "4:3", value: "4:3", w: 1472, h: 1140 },
  { label: "3:4", value: "3:4", w: 1140, h: 1472 },
]

const DURATIONS = [3, 5, 8, 10]
const FIRST_FRAME_MAX_BYTES = 20 * 1024 * 1024
const TASK_POLL_INTERVAL_MS = 8000
const TASK_MAX_POLLS = 90
const FIRST_FRAME_ACCEPT = "image/png,image/jpeg,image/webp"
const FIRST_FRAME_TYPES = new Set(["image/png", "image/jpeg", "image/webp"])
const VIDEO_HISTORY_STORAGE_KEY = "qwen2api_video_history_v1"
const VIDEO_HISTORY_LIMIT = 50
const VIDEO_HISTORY_AUTO_RESUME_MS = 24 * 60 * 60 * 1000

type GenerationMode = "t2v" | "i2v"
type VideoTaskStatus = "queued" | "running" | "succeeded" | "failed" | "interrupted" | "expired" | "uploading" | "creating" | "timeout" | string

interface GeneratedVideo {
  url: string
  revised_prompt: string
  ratio: string
  size: string
  width?: number
  height?: number
  duration?: number
  model?: string
  mode?: GenerationMode
}

interface VideoGenerationItem {
  url?: string
  revised_prompt?: string
  ratio?: string
  size?: string
  width?: number
  height?: number
  duration?: number
}

interface VideoGenerationResponse {
  data?: VideoGenerationItem[]
  detail?: unknown
  error?: unknown
}

interface VideoTaskResponse extends VideoGenerationResponse {
  id?: string
  object?: string
  status?: VideoTaskStatus
  model?: string
  mode?: string
  poll_url?: string
}

interface FileUploadResponse {
  id?: string
  detail?: unknown
  error?: unknown
}

interface VideoHistoryRecord {
  id: string
  taskId?: string
  status: VideoTaskStatus
  prompt: string
  model: string
  mode: GenerationMode
  ratio: string
  size: string
  duration: number
  n: number
  createdAt: number
  updatedAt: number
  error?: string
  urls: string[]
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, ms))
}

/** 将接口错误载荷整理成页面可读文本。 */
function formatErrorPayload(value: unknown): string {
  if (!value) return "未知错误"
  if (typeof value === "string") return value
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

/** 格式化首帧文件大小，便于上传区展示。 */
function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

/** 判断视频后台任务是否仍需继续轮询。 */
function isPendingTask(status?: VideoTaskStatus): boolean {
  return status === "queued" || status === "running"
}

/** 将后台任务状态转换成中文标签。 */
function formatTaskStatus(status?: VideoTaskStatus): string {
  switch (status) {
    case "uploading": return "上传首帧"
    case "creating": return "创建任务"
    case "queued": return "排队中"
    case "running": return "生成中"
    case "succeeded": return "已完成"
    case "failed": return "生成失败"
    case "interrupted": return "任务中断"
    case "expired": return "任务过期"
    case "timeout": return "轮询超时"
    default: return status || "等待开始"
  }
}

/** 从视频响应中提取有效 URL，供结果区和历史区复用。 */
function extractVideoUrls(items?: VideoGenerationItem[]): string[] {
  return (items ?? [])
    .map(item => item.url)
    .filter((url): url is string => typeof url === "string" && url.length > 0)
}

/** 读取本地生成历史，兼容 localStorage 缺失或数据损坏的情况。 */
function readVideoHistory(): VideoHistoryRecord[] {
  if (typeof window === "undefined") return []
  try {
    const raw = window.localStorage.getItem(VIDEO_HISTORY_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    const records: VideoHistoryRecord[] = []
    parsed.forEach(item => {
      if (!item || typeof item !== "object") return
      const record = item as Partial<VideoHistoryRecord>
      if (typeof record.id !== "string" || typeof record.prompt !== "string") return
      records.push({
        id: record.id,
        taskId: typeof record.taskId === "string" ? record.taskId : undefined,
        status: record.status || "queued",
        prompt: record.prompt,
        model: record.model || "unknown",
        mode: record.mode === "i2v" ? "i2v" : "t2v",
        ratio: record.ratio || "16:9",
        size: record.size || "",
        duration: Number(record.duration) || 0,
        n: Number(record.n) || 1,
        createdAt: Number(record.createdAt) || Date.now(),
        updatedAt: Number(record.updatedAt) || Date.now(),
        error: typeof record.error === "string" ? record.error : undefined,
        urls: Array.isArray(record.urls) ? record.urls.filter((url): url is string => typeof url === "string") : [],
      })
    })
    return records.slice(0, VIDEO_HISTORY_LIMIT)
  } catch {
    return []
  }
}

/** 写入本地生成历史，失败时静默处理，避免影响主流程。 */
function writeVideoHistory(records: VideoHistoryRecord[]) {
  if (typeof window === "undefined") return
  try {
    window.localStorage.setItem(VIDEO_HISTORY_STORAGE_KEY, JSON.stringify(records.slice(0, VIDEO_HISTORY_LIMIT)))
  } catch {
    // localStorage 可能被禁用或空间不足，主生成流程不应因此失败。
  }
}

/** 格式化历史创建时间，便于用户快速定位记录。 */
function formatHistoryTime(timestamp: number): string {
  if (!timestamp) return "-"
  return new Date(timestamp).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })
}

/** 根据任务状态返回历史徽标样式。 */
function getStatusBadgeClass(status?: VideoTaskStatus): string {
  if (status === "succeeded") return "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
  if (status === "failed" || status === "interrupted" || status === "expired" || status === "timeout") {
    return "bg-red-500/10 text-red-400 border-red-500/20"
  }
  if (isPendingTask(status)) return "bg-primary/10 text-primary border-primary/20"
  return "bg-muted text-muted-foreground border-border"
}

/** 判断历史任务是否仍在后端任务 TTL 内，避免恢复过旧轮询。 */
function isRecoverableHistoryTask(record: VideoHistoryRecord): boolean {
  const referenceTime = record.updatedAt || record.createdAt
  return isPendingTask(record.status) && Date.now() - referenceTime < VIDEO_HISTORY_AUTO_RESUME_MS
}

export default function VideoPage() {
  const resultsRef = useRef<HTMLDivElement | null>(null)
  const pollingTaskIdsRef = useRef<Set<string>>(new Set())
  const [prompt, setPrompt] = useState("")
  const [ratio, setRatio] = useState("16:9")
  const [duration, setDuration] = useState(5)
  const [n, setN] = useState(1)
  const [loading, setLoading] = useState(false)
  const [videos, setVideos] = useState<GeneratedVideo[]>([])
  const [error, setError] = useState<string | null>(null)
  const [model, setModel] = useState("qwen3.6-plus-video")
  const [videoModels, setVideoModels] = useState<ModelOption[]>(FALLBACK_VIDEO_MODELS)
  const [generationMode, setGenerationMode] = useState<GenerationMode>("t2v")
  const [firstFrameFile, setFirstFrameFile] = useState<File | null>(null)
  const [firstFramePreviewUrl, setFirstFramePreviewUrl] = useState("")
  const [dragActive, setDragActive] = useState(false)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [taskStatus, setTaskStatus] = useState<VideoTaskStatus | null>(null)
  const [taskError, setTaskError] = useState<string | null>(null)
  const [taskStartedAt, setTaskStartedAt] = useState<number | null>(null)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [historyRecords, setHistoryRecords] = useState<VideoHistoryRecord[]>([])

  const selectedRatio = ASPECT_RATIOS.find(r => r.value === ratio)!
  const sizeStr = `${selectedRatio.w}x${selectedRatio.h}`
  const groupedModels = groupModelOptions(videoModels)
  const isI2V = generationMode === "i2v"
  const canGenerate = Boolean(prompt.trim()) && !loading && (!isI2V || Boolean(firstFrameFile))

  useEffect(() => {
    (async () => {
      try {
        const options = filterVideoModels(await fetchModelOptions())
        setVideoModels(options)
        setModel(current => chooseDefaultModel(options, current, "qwen3.6-plus-video"))
      } catch {
        // keep fallback video model
      }
    })()
  }, [])

  useEffect(() => {
    if (!firstFrameFile) {
      setFirstFramePreviewUrl("")
      return
    }
    const objectUrl = URL.createObjectURL(firstFrameFile)
    setFirstFramePreviewUrl(objectUrl)
    return () => URL.revokeObjectURL(objectUrl)
  }, [firstFrameFile])

  useEffect(() => {
    if (!loading || !taskStartedAt) return
    const timer = window.setInterval(() => {
      setElapsedSeconds(Math.max(0, Math.floor((Date.now() - taskStartedAt) / 1000)))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [loading, taskStartedAt])

  /** 保存历史列表到状态和 localStorage，统一控制最多 50 条。 */
  const persistHistoryRecords = (records: VideoHistoryRecord[]) => {
    const next = records.slice(0, VIDEO_HISTORY_LIMIT)
    writeVideoHistory(next)
    return next
  }

  /** 插入或覆盖单条历史，任务创建后立即调用，防止页面关闭丢记录。 */
  const upsertHistoryRecord = (record: VideoHistoryRecord) => {
    setHistoryRecords(prev => {
      const next = [record, ...prev.filter(item => item.id !== record.id)]
      return persistHistoryRecords(next)
    })
  }

  /** 局部更新历史状态、URL 或错误信息。 */
  const updateHistoryRecord = (id: string, patch: Partial<VideoHistoryRecord>) => {
    setHistoryRecords(prev => {
      const next = prev.map(item => item.id === id
        ? { ...item, ...patch, updatedAt: patch.updatedAt ?? Date.now() }
        : item)
      return persistHistoryRecords(next)
    })
  }

  /** 清空本地历史，不影响后端任务和已经生成的视频文件。 */
  const clearHistoryRecords = () => {
    setHistoryRecords([])
    writeVideoHistory([])
    toast.success("已清空本地生成历史")
  }

  /** 校验并保存首帧图片，后续生成前再上传到 /v1/files。 */
  const selectFirstFrameFile = (file?: File | null) => {
    if (!file) return
    if (!FIRST_FRAME_TYPES.has(file.type)) {
      toast.error("首帧只支持 PNG、JPG 或 WebP 图片")
      return
    }
    if (file.size > FIRST_FRAME_MAX_BYTES) {
      toast.error("首帧图片不能超过 20MB")
      return
    }
    setFirstFrameFile(file)
    setError(null)
    setTaskError(null)
  }

  const handleFirstFrameChange = (event: ChangeEvent<HTMLInputElement>) => {
    selectFirstFrameFile(event.target.files?.[0])
    event.target.value = ""
  }

  const handleDropFirstFrame = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault()
    setDragActive(false)
    selectFirstFrameFile(event.dataTransfer.files?.[0])
  }

  /** 先把首帧图片上传到本地文件接口，返回后端 I2V 所需 file_id。 */
  const uploadFirstFrame = async (file: File): Promise<string> => {
    const formData = new FormData()
    formData.append("file", file, file.name)
    const res = await fetch(`${API_BASE}/v1/files`, {
      method: "POST",
      headers: getAuthHeader(),
      body: formData,
    })
    const data = (await res.json()) as FileUploadResponse
    if (!res.ok || !data.id) {
      throw new Error(formatErrorPayload(data.detail || data.error || `HTTP ${res.status}`))
    }
    return data.id
  }

  /** 单次查询异步视频任务，供主动生成和历史恢复复用。 */
  const fetchVideoTask = async (id: string): Promise<VideoTaskResponse> => {
    const res = await fetch(`${API_BASE}/v1/videos/tasks/${id}`, { headers: getAuthHeader() })
    const data = (await res.json()) as VideoTaskResponse
    if (!res.ok) {
      throw new Error(formatErrorPayload(data.detail || data.error || `HTTP ${res.status}`))
    }
    return data
  }

  /** 轮询异步视频任务，直到成功或终态失败。 */
  const pollVideoTask = async (
    id: string,
    onUpdate?: (task: VideoTaskResponse) => void,
  ): Promise<VideoTaskResponse> => {
    let latest: VideoTaskResponse = { id, status: "queued" }
    for (let index = 0; index < TASK_MAX_POLLS; index += 1) {
      await sleep(TASK_POLL_INTERVAL_MS)
      const data = await fetchVideoTask(id)
      latest = data
      onUpdate?.(data)
      if (data.status === "succeeded") return data
      if (!isPendingTask(data.status)) {
        throw new Error(formatErrorPayload(data.error || data.detail || `任务状态异常: ${data.status || "unknown"}`))
      }
    }
    throw new Error(`任务轮询超时: ${latest.id || id}`)
  }

  const appendGeneratedVideos = (
    items: VideoGenerationItem[] | undefined,
    context?: {
      prompt?: string
      ratio?: string
      size?: string
      duration?: number
      model?: string
      mode?: GenerationMode
      quiet?: boolean
    },
  ) => {
    const newVideos: GeneratedVideo[] = (items ?? [])
      .filter((item): item is VideoGenerationItem & { url: string } => typeof item.url === "string" && item.url.length > 0)
      .map(item => ({
        url: item.url,
        revised_prompt: item.revised_prompt || context?.prompt || prompt,
        ratio: item.ratio || context?.ratio || ratio,
        size: item.size || context?.size || sizeStr,
        width: item.width,
        height: item.height,
        duration: item.duration || context?.duration || duration,
        model: context?.model || model,
        mode: context?.mode || generationMode,
      }))

    if (newVideos.length === 0) {
      throw new Error("未返回视频，请重试")
    }

    setVideos(prev => {
      const existingUrls = new Set(prev.map(video => video.url))
      return [...newVideos.filter(video => !existingUrls.has(video.url)), ...prev]
    })
    if (!context?.quiet) {
      toast.success(`成功生成 ${newVideos.length} 个视频`)
    }
  }

  /** 将任务查询结果同步到本地历史。 */
  const syncTaskToHistory = (record: VideoHistoryRecord, task: VideoTaskResponse) => {
    const urls = extractVideoUrls(task.data)
    updateHistoryRecord(record.id, {
      status: task.status || "running",
      urls: urls.length > 0 ? urls : record.urls,
      error: task.error || task.detail ? formatErrorPayload(task.error || task.detail) : undefined,
    })
  }

  /** 把历史记录中的 URL 恢复到上方生成结果区，复用现有预览和下载能力。 */
  const restoreHistoryPreview = (record: VideoHistoryRecord) => {
    if (record.urls.length === 0) {
      toast.info("该历史记录还没有可预览的视频 URL")
      return
    }
    const restoredItems: VideoGenerationItem[] = record.urls.map(url => ({
      url,
      revised_prompt: record.prompt,
      ratio: record.ratio,
      size: record.size,
      duration: record.duration,
    }))
    appendGeneratedVideos(restoredItems, {
      prompt: record.prompt,
      ratio: record.ratio,
      size: record.size,
      duration: record.duration,
      model: record.model,
      mode: record.mode,
      quiet: true,
    })
    window.requestAnimationFrame(() => {
      resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })
    })
  }

  /** 恢复历史任务轮询，避免切换菜单或刷新后丢失还在生成的视频。 */
  const resumeHistoryTask = async (record: VideoHistoryRecord, manual = false) => {
    const id = record.taskId || record.id
    if (!id) return
    if (pollingTaskIdsRef.current.has(id)) {
      if (manual) toast.info("该任务正在查询中")
      return
    }

    pollingTaskIdsRef.current.add(id)
    try {
      const firstTask = await fetchVideoTask(id)
      syncTaskToHistory(record, firstTask)

      let finalTask = firstTask
      if (isPendingTask(firstTask.status)) {
        finalTask = await pollVideoTask(id, task => syncTaskToHistory(record, task))
      }

      if (finalTask.status === "succeeded") {
        syncTaskToHistory(record, finalTask)
        if (extractVideoUrls(finalTask.data).length > 0) {
          appendGeneratedVideos(finalTask.data, {
            prompt: record.prompt,
            ratio: record.ratio,
            size: record.size,
            duration: record.duration,
            model: record.model,
            mode: record.mode,
            quiet: !manual,
          })
        }
        if (manual) toast.success("历史任务已完成")
        return
      }

      if (!isPendingTask(finalTask.status)) {
        syncTaskToHistory(record, finalTask)
        if (manual) toast.error(`任务状态：${formatTaskStatus(finalTask.status)}`)
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "网络错误"
      updateHistoryRecord(record.id, {
        status: msg.includes("轮询超时") ? "timeout" : "failed",
        error: msg,
      })
      if (manual) toast.error(`查询失败: ${msg.slice(0, 80)}`)
    } finally {
      pollingTaskIdsRef.current.delete(id)
    }
  }

  useEffect(() => {
    const records = readVideoHistory()
    setHistoryRecords(records)
    records
      .filter(record => isRecoverableHistoryTask(record) && Boolean(record.taskId || record.id))
      .forEach(record => {
        void resumeHistoryTask(record)
      })
    // 初始化恢复只执行一次，避免每次历史更新都重复创建轮询。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleGenerate = async () => {
    if (!prompt.trim() || loading) return
    if (isI2V && !firstFrameFile) {
      toast.error("请先上传首帧图片")
      return
    }

    const requestPrompt = prompt.trim()
    const requestModel = model
    const requestMode = generationMode
    const requestRatio = ratio
    const requestSize = sizeStr
    const requestDuration = duration
    const requestN = n
    let currentHistoryId = ""

    setLoading(true)
    setError(null)
    setTaskError(null)
    setTaskId(null)
    setTaskStatus(isI2V ? "uploading" : "creating")
    setTaskStartedAt(Date.now())
    setElapsedSeconds(0)

    try {
      let fileId = ""
      if (isI2V && firstFrameFile) {
        fileId = await uploadFirstFrame(firstFrameFile)
      }

      setTaskStatus("creating")
      const res = await fetch(`${API_BASE}/v1/videos/generations`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeader() },
        body: JSON.stringify({
          model: requestModel,
          prompt: requestPrompt,
          n: requestN,
          size: requestSize,
          ratio: requestRatio,
          aspect_ratio: requestRatio,
          width: selectedRatio.w,
          height: selectedRatio.h,
          duration: requestDuration,
          response_format: "url",
          async: true,
          ...(fileId ? { file_id: fileId } : {}),
        }),
      })

      const data = (await res.json()) as VideoTaskResponse
      if (!res.ok) {
        throw new Error(formatErrorPayload(data.detail || data.error || `HTTP ${res.status}`))
      }

      // 兼容旧服务直接同步返回 data[] 的情况，但新服务会返回 video_task_xxx。
      if (!data.id && data.data) {
        const urls = extractVideoUrls(data.data)
        const fallbackRecord: VideoHistoryRecord = {
          id: `local_${Date.now()}`,
          status: "succeeded",
          prompt: requestPrompt,
          model: requestModel,
          mode: requestMode,
          ratio: requestRatio,
          size: requestSize,
          duration: requestDuration,
          n: requestN,
          createdAt: Date.now(),
          updatedAt: Date.now(),
          urls,
        }
        upsertHistoryRecord(fallbackRecord)
        appendGeneratedVideos(data.data, {
          prompt: requestPrompt,
          ratio: requestRatio,
          size: requestSize,
          duration: requestDuration,
          model: requestModel,
          mode: requestMode,
        })
        setTaskStatus("succeeded")
        return
      }
      if (!data.id) {
        throw new Error("创建视频任务失败：未返回任务 ID")
      }

      currentHistoryId = data.id
      const historyRecord: VideoHistoryRecord = {
        id: data.id,
        taskId: data.id,
        status: data.status || "queued",
        prompt: requestPrompt,
        model: requestModel,
        mode: requestMode,
        ratio: requestRatio,
        size: requestSize,
        duration: requestDuration,
        n: requestN,
        createdAt: Date.now(),
        updatedAt: Date.now(),
        urls: [],
      }
      upsertHistoryRecord(historyRecord)
      setTaskId(data.id)
      setTaskStatus(data.status || "queued")

      pollingTaskIdsRef.current.add(data.id)
      const finalTask = data.status === "succeeded"
        ? data
        : await pollVideoTask(data.id, task => {
          setTaskStatus(task.status || "running")
          syncTaskToHistory(historyRecord, task)
        })
      pollingTaskIdsRef.current.delete(data.id)

      const finalUrls = extractVideoUrls(finalTask.data)
      updateHistoryRecord(data.id, {
        status: "succeeded",
        urls: finalUrls,
        error: undefined,
      })
      appendGeneratedVideos(finalTask.data, {
        prompt: requestPrompt,
        ratio: requestRatio,
        size: requestSize,
        duration: requestDuration,
        model: requestModel,
        mode: requestMode,
      })
      setTaskStatus("succeeded")
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "网络错误"
      if (currentHistoryId) {
        updateHistoryRecord(currentHistoryId, {
          status: msg.includes("轮询超时") ? "timeout" : "failed",
          error: msg,
        })
        pollingTaskIdsRef.current.delete(currentHistoryId)
      }
      setError(msg)
      setTaskError(msg)
      toast.error(`生成失败: ${msg.slice(0, 80)}`)
    } finally {
      setLoading(false)
    }
  }

  const handleDownload = (url: string, idx: number) => {
    const a = document.createElement("a")
    a.href = url
    a.download = `qwen_video_${Date.now()}_${idx}.mp4`
    a.target = "_blank"
    a.rel = "noopener noreferrer"
    a.click()
  }

  return (
    <div className="w-full space-y-6">
      <section className="admin-hero p-6">
        <div className="relative z-10">
          <div className="text-xs font-black uppercase tracking-[0.28em] text-muted-foreground">Video Lab</div>
          <h2 className="mt-2 text-4xl font-black tracking-tight">视频生成</h2>
          <p className="mt-2 text-muted-foreground">选择视频模型生成短视频，支持文生视频、首帧图生视频、比例、时长、任务轮询和结果下载。</p>
        </div>
      </section>

      <div className="admin-card p-6 space-y-4">
        <div className="grid grid-cols-2 gap-2 rounded-lg border bg-muted/30 p-1">
          {([
            { value: "t2v" as GenerationMode, title: "文生视频", desc: "只根据提示词生成" },
            { value: "i2v" as GenerationMode, title: "首帧图生视频", desc: "上传图片作为第一帧" },
          ]).map(item => (
            <button
              key={item.value}
              type="button"
              onClick={() => setGenerationMode(item.value)}
              disabled={loading}
              className={`rounded-md px-3 py-2 text-left transition-all ${
                generationMode === item.value
                  ? "bg-primary text-primary-foreground shadow-sm"
                  : "text-muted-foreground hover:bg-background hover:text-foreground"
              }`}
            >
              <div className="text-sm font-medium">{item.title}</div>
              <div className="text-xs opacity-75">{item.desc}</div>
            </button>
          ))}
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium">视频描述 (Prompt)</label>
          <textarea
            rows={3}
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
            placeholder={isI2V ? "描述首帧之后的动作，例如：保持同一人物和构图，镜头缓慢推进，主体轻微运动" : "描述你想生成的视频，例如：雨夜霓虹街头，一只黑猫慢慢穿过水洼，电影感镜头"}
            className="admin-input flex w-full px-3 py-2 text-sm resize-none"
            disabled={loading}
            onKeyDown={e => {
              if (e.key === "Enter" && e.ctrlKey) handleGenerate()
            }}
          />
          <p className="text-xs text-muted-foreground">Ctrl+Enter 快速生成</p>
        </div>

        {isI2V && (
          <div className="space-y-2">
            <label className="text-sm font-medium">首帧图片</label>
            <label
              onDragEnter={event => {
                event.preventDefault()
                setDragActive(true)
              }}
              onDragOver={event => event.preventDefault()}
              onDragLeave={event => {
                event.preventDefault()
                setDragActive(false)
              }}
              onDrop={handleDropFirstFrame}
              className={`flex cursor-pointer flex-col gap-3 rounded-xl border border-dashed p-4 transition-colors ${
                dragActive ? "border-primary bg-primary/10" : "border-border bg-muted/20 hover:border-primary/50 hover:bg-muted/30"
              }`}
            >
              <input
                type="file"
                accept={FIRST_FRAME_ACCEPT}
                className="hidden"
                disabled={loading}
                onChange={handleFirstFrameChange}
              />
              {firstFrameFile && firstFramePreviewUrl ? (
                <div className="flex gap-4">
                  <img src={firstFramePreviewUrl} alt="首帧预览" className="h-28 w-40 rounded-lg object-cover border bg-background" />
                  <div className="min-w-0 flex-1 space-y-2">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium">{firstFrameFile.name}</p>
                        <p className="text-xs text-muted-foreground">{firstFrameFile.type} · {formatBytes(firstFrameFile.size)}</p>
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={loading}
                        onClick={event => {
                          event.preventDefault()
                          setFirstFrameFile(null)
                        }}
                        className="gap-1.5"
                      >
                        <X className="h-3.5 w-3.5" /> 移除
                      </Button>
                    </div>
                    <p className="text-xs text-muted-foreground">生成时会先上传到 /v1/files，再作为 I2V 首帧 file_id 提交。</p>
                  </div>
                </div>
              ) : (
                <div className="flex items-center gap-3 text-muted-foreground">
                  <div className="rounded-lg border bg-background p-3">
                    <UploadCloud className="h-5 w-5" />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-foreground">点击或拖拽上传首帧图片</p>
                    <p className="text-xs">支持 PNG / JPG / WebP，最大 20MB。首帧将作为生成视频的第一帧。</p>
                  </div>
                </div>
              )}
            </label>
            <p className="text-xs text-muted-foreground">I2V 模式可使用 qwen-i2v 或实测通过的 *-video 模型，后端会自动切换 chat_type=i2v。</p>
          </div>
        )}

        <div className="flex flex-wrap gap-4 items-end">
          <div className="space-y-1.5 min-w-[260px]">
            <label className="text-sm font-medium">视频模型</label>
            <select
              value={model}
              onChange={e => setModel(e.target.value)}
              className="admin-input h-10 w-full px-3 py-2 text-sm font-mono"
              disabled={loading}
            >
              {groupedModels.map(group => (
                <optgroup key={group.family} label={group.family}>
                  {group.models.map(option => (
                    <option key={option.id} value={option.id}>{formatModelOptionLabel(option)}</option>
                  ))}
                </optgroup>
              ))}
            </select>
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium">视频比例</label>
            <div className="flex gap-2">
              {ASPECT_RATIOS.map(r => (
                <button
                  key={r.value}
                  onClick={() => setRatio(r.value)}
                  className={`px-3 py-1.5 rounded-md text-sm font-medium border transition-all ${
                    ratio === r.value
                      ? "bg-primary text-primary-foreground border-primary shadow-sm"
                      : "bg-background/70 border-border text-muted-foreground hover:text-foreground hover:border-foreground/30"
                  }`}
                  disabled={loading}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium">视频时长</label>
            <div className="flex gap-2">
              {DURATIONS.map(v => (
                <button
                  key={v}
                  onClick={() => setDuration(v)}
                  className={`px-3 py-1.5 rounded-md text-sm font-medium border transition-all ${
                    duration === v
                      ? "bg-primary text-primary-foreground border-primary shadow-sm"
                      : "bg-background/70 border-border text-muted-foreground hover:text-foreground hover:border-foreground/30"
                  }`}
                  disabled={loading}
                >
                  {v}s
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium">生成数量</label>
            <div className="flex gap-2">
              {[1, 2].map(v => (
                <button
                  key={v}
                  onClick={() => setN(v)}
                  className={`px-3 py-1.5 rounded-md text-sm font-medium border transition-all ${
                    n === v
                      ? "bg-primary text-primary-foreground border-primary shadow-sm"
                      : "bg-background/70 border-border text-muted-foreground hover:text-foreground hover:border-foreground/30"
                  }`}
                  disabled={loading}
                >
                  {v} 个
                </button>
              ))}
            </div>
          </div>

          <div className="admin-chip font-mono">
            {sizeStr}
          </div>

          <Button
            onClick={handleGenerate}
            disabled={!canGenerate}
            className="ml-auto h-10 px-6 gap-2"
          >
            {loading
              ? <><RefreshCw className="h-4 w-4 animate-spin" /> 生成中...</>
              : <><Wand2 className="h-4 w-4" /> 生成视频</>
            }
          </Button>
        </div>

        {isI2V && !firstFrameFile && (
          <div className="rounded-md bg-primary/10 border border-primary/20 text-primary px-4 py-3 text-sm">
            首帧图生视频模式需要先上传一张首帧图片。
          </div>
        )}

        {error && (
          <div className="rounded-md bg-red-500/10 border border-red-500/30 text-red-400 px-4 py-3 text-sm">
            {error}
          </div>
        )}
      </div>

      {loading && (
        <div className="admin-card p-8">
          <div className="flex flex-col items-center justify-center gap-4 text-muted-foreground">
            <div className="relative">
              <Film className="h-16 w-16 text-muted-foreground/20" />
              <RefreshCw className="h-6 w-6 animate-spin absolute -bottom-1 -right-1 text-primary" />
            </div>
            <div className="space-y-2 text-center">
              <p className="font-medium">正在生成视频...</p>
              <p className="text-sm text-muted-foreground/70">{formatTaskStatus(taskStatus || undefined)} · 已等待 {elapsedSeconds}s</p>
              {taskId && <p className="text-xs font-mono text-muted-foreground/70">任务 ID：{taskId}</p>}
              <p className="text-xs text-muted-foreground/60">页面会每 8 秒自动轮询；任务 ID 已写入本地历史，切换菜单后可恢复查询</p>
            </div>
          </div>
        </div>
      )}

      {taskError && !loading && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-400">
          任务错误：{taskError}
        </div>
      )}

      {videos.length > 0 && !loading && (
        <div ref={resultsRef} className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold">生成结果 ({videos.length} 个)</h3>
            <Button variant="ghost" size="sm" onClick={() => setVideos([])}>
              清空
            </Button>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {videos.map((video, idx) => (
              <div key={`${video.url}-${idx}`} className="admin-card overflow-hidden group">
                <div className="relative bg-muted/30">
                  <video
                    src={video.url}
                    controls
                    className="w-full aspect-video bg-black object-contain"
                    preload="metadata"
                  />
                  <div className="absolute top-3 right-3 flex gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
                    <Button size="sm" variant="secondary" onClick={() => handleDownload(video.url, idx)} className="gap-1.5">
                      <Download className="h-3.5 w-3.5" /> 下载
                    </Button>
                    <Button size="sm" variant="secondary" onClick={() => window.open(video.url, "_blank")}>
                      打开
                    </Button>
                  </div>
                </div>
                <div className="p-3 space-y-1">
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="admin-chip font-mono">{video.mode === "i2v" ? "I2V" : "T2V"}</span>
                    <span className="admin-chip font-mono">{video.ratio}</span>
                    <span className="admin-chip font-mono">{video.duration || duration}s</span>
                    <span className="admin-chip font-mono">请求 {video.size}</span>
                    {video.model && <span className="admin-chip font-mono">{video.model}</span>}
                    <span className="truncate">{video.revised_prompt.slice(0, 80)}</span>
                  </div>
                  <div className="text-xs text-muted-foreground font-mono truncate">{video.url}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {videos.length === 0 && !loading && (
        <div className="admin-card p-12">
          <div className="flex flex-col items-center gap-4 text-muted-foreground">
            {isI2V ? <ImageIcon className="h-16 w-16 text-muted-foreground/20" /> : <VideoIcon className="h-16 w-16 text-muted-foreground/20" />}
            <div className="text-center">
              <p className="font-medium">还没有生成视频</p>
              <p className="text-sm text-muted-foreground/70 mt-1">在上方输入描述，点击「生成视频」开始创作</p>
            </div>
          </div>
        </div>
      )}

      <div className="rounded-xl border bg-card shadow-sm p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <div className="rounded-lg border bg-muted/30 p-2 text-muted-foreground">
              <History className="h-4 w-4" />
            </div>
            <div>
              <h3 className="font-semibold">生成历史</h3>
              <p className="text-xs text-muted-foreground">本地保存最近 {VIDEO_HISTORY_LIMIT} 条任务，刷新页面或切换菜单后可继续查询。</p>
            </div>
          </div>
          {historyRecords.length > 0 && (
            <Button variant="ghost" size="sm" onClick={clearHistoryRecords} className="gap-1.5 text-muted-foreground">
              <Trash2 className="h-3.5 w-3.5" /> 清空历史
            </Button>
          )}
        </div>

        {historyRecords.length === 0 ? (
          <div className="rounded-lg border border-dashed bg-muted/20 p-6 text-center text-sm text-muted-foreground">
            暂无生成历史。创建异步视频任务后，任务 ID 会立即写入这里。
          </div>
        ) : (
          <div className="space-y-3">
            {historyRecords.map(record => (
              <div key={record.id} className="rounded-lg border bg-background/60 p-4 space-y-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 space-y-2">
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <span className={`rounded-full border px-2 py-0.5 font-medium ${getStatusBadgeClass(record.status)}`}>
                        {formatTaskStatus(record.status)}
                      </span>
                      <span className="rounded bg-muted px-1.5 py-0.5 font-mono">{record.mode === "i2v" ? "I2V" : "T2V"}</span>
                      <span className="rounded bg-muted px-1.5 py-0.5 font-mono">{record.ratio}</span>
                      <span className="rounded bg-muted px-1.5 py-0.5 font-mono">{record.duration}s</span>
                      <span className="rounded bg-muted px-1.5 py-0.5 font-mono">{record.n} 个</span>
                      <span className="rounded bg-muted px-1.5 py-0.5 font-mono">{record.model}</span>
                    </div>
                    <p className="line-clamp-2 text-sm text-foreground">{record.prompt}</p>
                    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                      <span>创建：{formatHistoryTime(record.createdAt)}</span>
                      <span>更新：{formatHistoryTime(record.updatedAt)}</span>
                      <span className="font-mono">任务 ID：{record.taskId || record.id}</span>
                    </div>
                  </div>

                  <div className="flex shrink-0 gap-2">
                    {(record.taskId || record.id) && record.status !== "succeeded" && (
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        onClick={() => void resumeHistoryTask(record, true)}
                        className="gap-1.5"
                      >
                        <RefreshCw className="h-3.5 w-3.5" /> 重新查询
                      </Button>
                    )}
                    {record.urls.length > 0 && (
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        onClick={() => restoreHistoryPreview(record)}
                      >
                        预览
                      </Button>
                    )}
                  </div>
                </div>

                {record.urls.length > 0 && (
                  <div className="space-y-2">
                    {record.urls.map((url, index) => (
                      <div key={`${record.id}-${url}`} className="flex items-center gap-2 rounded-md border bg-muted/20 px-3 py-2">
                        <button
                          type="button"
                          onClick={() => restoreHistoryPreview(record)}
                          className="min-w-0 flex-1 truncate text-left text-xs font-mono text-primary hover:underline"
                          title="点击恢复到上方生成结果预览区"
                        >
                          {url}
                        </button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() => handleDownload(url, index)}
                          className="h-7 gap-1.5 px-2 text-xs"
                        >
                          <Download className="h-3.5 w-3.5" /> 下载
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() => window.open(url, "_blank")}
                          className="h-7 gap-1.5 px-2 text-xs"
                        >
                          <ExternalLink className="h-3.5 w-3.5" /> 打开
                        </Button>
                      </div>
                    ))}
                  </div>
                )}

                {record.error && (
                  <div className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-400">
                    {record.error}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

    </div>
  )
}
