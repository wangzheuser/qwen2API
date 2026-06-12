import { useEffect, useState } from "react"
import { Image as ImageIcon, RefreshCw, Download, Wand2 } from "lucide-react"
import { Button } from "../components/ui/button"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import {
  FALLBACK_IMAGE_MODELS,
  chooseDefaultModel,
  fetchModelOptions,
  filterImageModels,
  formatModelOptionLabel,
  groupModelOptions,
  type ModelOption,
} from "../lib/models"

const ASPECT_RATIOS = [
  { label: "1:1",  value: "1:1",   w: 1328, h: 1328 },
  { label: "16:9", value: "16:9",  w: 1664, h: 928  },
  { label: "9:16", value: "9:16",  w: 928,  h: 1664 },
  { label: "4:3",  value: "4:3",   w: 1472, h: 1140 },
  { label: "3:4",  value: "3:4",   w: 1140, h: 1472 },
]

interface GeneratedImage {
  url: string
  revised_prompt: string
  ratio: string
  size: string
  width?: number
  height?: number
  naturalWidth?: number
  naturalHeight?: number
  model?: string
}

interface ImageGenerationItem {
  url?: string
  revised_prompt?: string
  ratio?: string
  size?: string
  width?: number
  height?: number
}

interface ImageGenerationResponse {
  data?: ImageGenerationItem[]
  detail?: unknown
  error?: unknown
}

export default function ImagePage() {
  const [prompt, setPrompt] = useState("")
  const [ratio, setRatio] = useState("1:1")
  const [n, setN] = useState(1)
  const [loading, setLoading] = useState(false)
  const [images, setImages] = useState<GeneratedImage[]>([])
  const [error, setError] = useState<string | null>(null)
  const [model, setModel] = useState("qwen3.7-plus-image")
  const [imageModels, setImageModels] = useState<ModelOption[]>(FALLBACK_IMAGE_MODELS)

  const selectedRatio = ASPECT_RATIOS.find(r => r.value === ratio)!
  const sizeStr = `${selectedRatio.w}x${selectedRatio.h}`
  const groupedModels = groupModelOptions(imageModels, { preserveOrder: true })

  useEffect(() => {
    (async () => {
      try {
        const options = filterImageModels(await fetchModelOptions())
        setImageModels(options)
        setModel(current => chooseDefaultModel(options, current, "qwen3.7-plus-image"))
      } catch {
        // keep fallback image model
      }
    })()
  }, [])

  const handleGenerate = async () => {
    if (!prompt.trim() || loading) return
    setLoading(true)
    setError(null)

    try {
      const res = await fetch(`${API_BASE}/v1/images/generations`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeader() },
        body: JSON.stringify({
          model,
          prompt: prompt.trim(),
          n,
          size: sizeStr,
          ratio,
          aspect_ratio: ratio,
          width: selectedRatio.w,
          height: selectedRatio.h,
          response_format: "url",
        }),
      })

      const data = (await res.json()) as ImageGenerationResponse
      if (!res.ok) {
        const detail = data?.detail || data?.error || `HTTP ${res.status}`
        setError(String(detail))
        toast.error(`生成失败: ${String(detail).slice(0, 80)}`)
        return
      }

      const newImages: GeneratedImage[] = (data.data ?? [])
        .filter((item): item is ImageGenerationItem & { url: string } => typeof item.url === "string" && item.url.length > 0)
        .map(item => ({
          url: item.url,
          revised_prompt: item.revised_prompt || prompt,
          ratio: item.ratio || ratio,
          size: item.size || sizeStr,
          width: item.width,
          height: item.height,
          model,
        }))

      if (newImages.length === 0) {
        setError("未返回图片，请重试")
        toast.error("未返回图片，请重试")
        return
      }

      setImages(prev => [...newImages, ...prev])
      toast.success(`成功生成 ${newImages.length} 张图片`)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "网络错误"
      setError(msg)
      toast.error(`生成失败: ${msg}`)
    } finally {
      setLoading(false)
    }
  }

  const handleDownload = (url: string, idx: number) => {
    const a = document.createElement("a")
    a.href = url
    a.download = `qwen_image_${Date.now()}_${idx}.png`
    a.target = "_blank"
    a.rel = "noopener noreferrer"
    a.click()
  }

  const handleImageLoad = (url: string, image: HTMLImageElement) => {
    setImages(prev => prev.map(item => (
      item.url === url
        ? { ...item, naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight }
        : item
    )))
  }

  const formatActualSize = (img: GeneratedImage) => {
    if (!img.naturalWidth || !img.naturalHeight) return "实际尺寸检测中"
    return `${img.naturalWidth}x${img.naturalHeight}`
  }

  const getRatioStatus = (img: GeneratedImage) => {
    if (!img.naturalWidth || !img.naturalHeight || !img.width || !img.height) return null
    const expected = img.width / img.height
    const actual = img.naturalWidth / img.naturalHeight
    const diff = Math.abs(expected - actual) / expected
    return diff <= 0.03 ? "比例匹配" : `实际比例 ${(actual).toFixed(2)}，与请求 ${img.ratio} 不一致`
  }

  return (
    <div className="space-y-6 max-w-5xl">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">图片生成</h2>
        <p className="text-muted-foreground">选择图片模型生成 AI 图片，支持多种比例。</p>
      </div>

      {/* 输入区域 */}
      <div className="rounded-xl border bg-card shadow-sm p-6 space-y-4">
        <div className="space-y-2">
          <label className="text-sm font-medium">图片描述 (Prompt)</label>
          <textarea
            rows={3}
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
            placeholder="描述你想生成的图片，例如：赛博朋克风格的猫咪，霓虹灯背景，超写实风格"
            className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm resize-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            disabled={loading}
            onKeyDown={e => {
              if (e.key === "Enter" && e.ctrlKey) handleGenerate()
            }}
          />
          <p className="text-xs text-muted-foreground">Ctrl+Enter 快速生成</p>
        </div>

        <div className="flex flex-wrap gap-4 items-end">
          <div className="space-y-1.5 min-w-[260px]">
            <label className="text-sm font-medium">图片模型</label>
            <select
              value={model}
              onChange={e => setModel(e.target.value)}
              className="h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-mono outline-none focus-visible:ring-1 focus-visible:ring-ring"
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

          {/* 比例选择 */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">图片比例</label>
            <div className="flex gap-2">
              {ASPECT_RATIOS.map(r => (
                <button
                  key={r.value}
                  onClick={() => setRatio(r.value)}
                  className={`px-3 py-1.5 rounded-md text-sm font-medium border transition-all ${
                    ratio === r.value
                      ? "bg-primary text-primary-foreground border-primary shadow-sm"
                      : "bg-background border-border text-muted-foreground hover:text-foreground hover:border-foreground/30"
                  }`}
                  disabled={loading}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </div>

          {/* 数量选择 */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">生成数量</label>
            <div className="flex gap-2">
              {[1, 2, 4].map(v => (
                <button
                  key={v}
                  onClick={() => setN(v)}
                  className={`px-3 py-1.5 rounded-md text-sm font-medium border transition-all ${
                    n === v
                      ? "bg-primary text-primary-foreground border-primary shadow-sm"
                      : "bg-background border-border text-muted-foreground hover:text-foreground hover:border-foreground/30"
                  }`}
                  disabled={loading}
                >
                  {v} 张
                </button>
              ))}
            </div>
          </div>

          {/* 尺寸预览 */}
          <div className="text-xs text-muted-foreground font-mono bg-muted/50 border rounded-md px-2 py-1">
            {sizeStr}
          </div>

          {/* 生成按钮 */}
          <Button
            onClick={handleGenerate}
            disabled={loading || !prompt.trim()}
            className="ml-auto h-10 px-6 gap-2"
          >
            {loading
              ? <><RefreshCw className="h-4 w-4 animate-spin" /> 生成中...</>
              : <><Wand2 className="h-4 w-4" /> 生成图片</>
            }
          </Button>
        </div>

        {/* 错误提示 */}
        {error && (
          <div className="rounded-md bg-red-500/10 border border-red-500/30 text-red-400 px-4 py-3 text-sm">
            {error}
          </div>
        )}
      </div>

      {/* 加载状态占位 */}
      {loading && (
        <div className="rounded-xl border bg-card shadow-sm p-8">
          <div className="flex flex-col items-center justify-center gap-4 text-muted-foreground">
            <div className="relative">
              <ImageIcon className="h-16 w-16 text-muted-foreground/20" />
              <RefreshCw className="h-6 w-6 animate-spin absolute -bottom-1 -right-1 text-primary" />
            </div>
            <div className="text-center">
              <p className="font-medium">正在生成图片...</p>
              <p className="text-sm text-muted-foreground/70 mt-1">图片生成通常需要 10-30 秒，请耐心等待</p>
            </div>
          </div>
        </div>
      )}

      {/* 图片展示区 */}
      {images.length > 0 && !loading && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold">生成结果 ({images.length} 张)</h3>
            <Button variant="ghost" size="sm" onClick={() => setImages([])}>
              清空
            </Button>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {images.map((img, idx) => (
              <div key={`${img.url}-${idx}`} className="rounded-xl border bg-card shadow-sm overflow-hidden group">
                <div className="relative bg-muted/30">
                  <img
                    src={img.url}
                    alt={img.revised_prompt}
                    className="w-full h-auto object-contain"
                    loading="lazy"
                    onLoad={e => handleImageLoad(img.url, e.currentTarget)}
                    onError={e => {
                      const target = e.currentTarget
                      target.style.display = "none"
                      target.nextElementSibling?.classList.remove("hidden")
                    }}
                  />
                  <div className="hidden items-center justify-center p-8 text-muted-foreground text-sm">
                    <ImageIcon className="h-8 w-8 mr-2" /> 图片加载失败
                  </div>
                  {/* 悬浮操作栏 */}
                  <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-3">
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => handleDownload(img.url, idx)}
                      className="gap-1.5"
                    >
                      <Download className="h-3.5 w-3.5" /> 下载
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => window.open(img.url, "_blank")}
                    >
                      在新窗口打开
                    </Button>
                  </div>
                </div>
                <div className="p-3 space-y-1">
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="bg-muted rounded px-1.5 py-0.5 font-mono">{img.ratio}</span>
                    <span className="bg-muted rounded px-1.5 py-0.5 font-mono">请求 {img.size}</span>
                    {img.model && <span className="bg-muted rounded px-1.5 py-0.5 font-mono">{img.model}</span>}
                    <span className="bg-muted rounded px-1.5 py-0.5 font-mono">实际 {formatActualSize(img)}</span>
                    <span className="truncate">{img.revised_prompt.slice(0, 80)}</span>
                  </div>
                  {getRatioStatus(img) && (
                    <div className={`text-xs ${getRatioStatus(img) === "比例匹配" ? "text-emerald-500" : "text-amber-500"}`}>
                      {getRatioStatus(img)}
                    </div>
                  )}
                  <div className="text-xs text-muted-foreground font-mono truncate">{img.url}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 空状态 */}
      {images.length === 0 && !loading && (
        <div className="rounded-xl border bg-card/50 shadow-sm p-12">
          <div className="flex flex-col items-center gap-4 text-muted-foreground">
            <ImageIcon className="h-16 w-16 text-muted-foreground/20" />
            <div className="text-center">
              <p className="font-medium">还没有生成图片</p>
              <p className="text-sm text-muted-foreground/70 mt-1">在上方输入描述，点击「生成图片」开始创作</p>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
