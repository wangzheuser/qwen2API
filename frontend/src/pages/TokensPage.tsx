import { useCallback, useEffect, useMemo, useState } from "react"
import { Button } from "../components/ui/button"
import { Check, Copy, KeyRound, Plus, RefreshCw, ShieldCheck, Trash2, X } from "lucide-react"
import { toast } from "sonner"
import { adminRequestErrorMessage, getAuthHeader, getStoredApiKey } from "../lib/auth"
import { API_BASE } from "../lib/api"

type ApiKeyItem = {
  key: string
  source?: "env" | "managed"
  label?: string
}

function maskKey(key: string) {
  if (key.length <= 14) return "sk-***"
  return `${key.slice(0, 8)}...${key.slice(-6)}`
}

export default function TokensPage() {
  const [keys, setKeys] = useState<ApiKeyItem[]>([])
  const [copied, setCopied] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)
  const [createMode, setCreateMode] = useState<"auto" | "custom">("auto")
  const [customKey, setCustomKey] = useState("")

  const latestKey = useMemo(() => keys[0]?.key || "", [keys])

  const loadKeys = useCallback(() => {
    if (!getStoredApiKey()) {
      setKeys([])
      setLoading(false)
      toast.error("请先到「系统设置」粘贴 ADMIN_KEY 或 data/api_keys.json 中已有 API Key")
      return
    }
    setLoading(true)
    fetch(`${API_BASE}/api/admin/keys`, { headers: getAuthHeader() })
      .then(async res => {
        if (!res.ok) throw new Error(await adminRequestErrorMessage(res))
        return res.json()
      })
      .then(data => {
        if (Array.isArray(data.items)) {
          setKeys(data.items)
          return
        }
        setKeys((data.keys || []).map((key: string) => ({ key, source: "managed", label: "面板创建 Key" })))
      })
      .catch(err => toast.error(err instanceof Error ? err.message : "刷新失败，请检查会话 Key"))
      .finally(() => setLoading(false))
  }, [])

  const fetchKeys = useCallback(() => {
    loadKeys()
  }, [loadKeys])

  useEffect(() => {
    loadKeys()
  }, [loadKeys])

  /**
   * 复制完整 Key，并返回复制是否成功。
   */
  const copyToClipboard = async (text: string, notifySuccess = true): Promise<boolean> => {
    const value = text.trim()
    if (!value) {
      toast.error("没有可复制的内容")
      return false
    }
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value)
      } else {
        throw new Error("clipboard unavailable")
      }
    } catch {
      try {
        const textarea = document.createElement("textarea")
        textarea.value = value
        textarea.setAttribute("readonly", "")
        textarea.style.position = "fixed"
        textarea.style.left = "-9999px"
        document.body.appendChild(textarea)
        textarea.select()
        // 在非安全上下文中回退到传统复制接口，兼容 HTTP 部署。
        const ok = document.execCommand("copy")
        document.body.removeChild(textarea)
        if (!ok) {
          throw new Error("copy failed")
        }
      } catch {
        toast.error("复制失败，请检查浏览器剪贴板权限（HTTP 环境可能受限，可改用 HTTPS 或 localhost 访问）")
        return false
      }
    }
    setCopied(value)
    if (notifySuccess) {
      toast.success("已复制到剪贴板")
    }
    window.setTimeout(() => setCopied(null), 1800)
    return true
  }

  const handleCreate = () => {
    if (!getStoredApiKey()) {
      toast.error("请先到「系统设置」粘贴 ADMIN_KEY 或已有 API Key")
      return
    }
    if (createMode === "custom" && !customKey.trim()) {
      toast.error("请输入自定义 API Key")
      return
    }
    const id = toast.loading(createMode === "custom" ? "正在添加自定义 API Key..." : "正在生成新的 API Key...")
    fetch(`${API_BASE}/api/admin/keys`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeader() },
      body: JSON.stringify({
        mode: createMode,
        key: createMode === "custom" ? customKey.trim() : "",
      }),
    }).then(async res => {
      const data = await res.json().catch(() => ({}))
      if (res.ok) {
        const copiedKey = data.key ? await copyToClipboard(data.key, false) : false
        toast.success(createMode === "custom" ? "自定义 API Key 已添加" : copiedKey ? "已生成新的 API Key，并复制到剪贴板" : "已生成新的 API Key，请从列表手动复制", { id })
        setCreateOpen(false)
        setCustomKey("")
        setCreateMode("auto")
        fetchKeys()
      } else {
        toast.error(data.detail || data.error || "创建失败，请检查权限", { id })
      }
    }).catch(() => toast.error("创建失败，请检查权限", { id }))
  }

  const handleDelete = (item: ApiKeyItem) => {
    if (!getStoredApiKey()) {
      toast.error("请先到「系统设置」粘贴 ADMIN_KEY 或已有 API Key")
      return
    }
    if (item.source === "env") {
      toast.error("环境变量注入 Key 不能在面板删除")
      return
    }
    const id = toast.loading("正在删除 API Key...")
    fetch(`${API_BASE}/api/admin/keys/${encodeURIComponent(item.key)}`, {
      method: "DELETE",
      headers: getAuthHeader(),
    }).then(async res => {
      if (res.ok) {
        toast.success("API Key 已删除", { id })
        fetchKeys()
      } else {
        toast.error(await adminRequestErrorMessage(res), { id })
      }
    }).catch(() => toast.error("删除失败", { id }))
  }

  return (
    <div className="space-y-6">
      <section className="relative overflow-hidden rounded-[32px] border border-white/75 bg-card/82 p-6 shadow-[var(--shadow-lift)] backdrop-blur-sm">
        <div className="pointer-events-none absolute -right-16 -top-20 size-56 rounded-full bg-accent/45 blur-3xl" />
        <div className="relative flex flex-col justify-between gap-4 lg:flex-row lg:items-end">
          <div>
            <div className="text-xs font-black uppercase tracking-[0.28em] text-muted-foreground">API Key</div>
            <h2 className="mt-2 text-4xl font-black tracking-tight">API Key 分发</h2>
            <p className="mt-2 max-w-2xl text-muted-foreground">
              管理下游客户端访问 Go 网关的 Bearer Key，适配 OpenAI、Anthropic、Gemini、图片、视频和文件接口。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => { fetchKeys(); toast.success("已刷新") }} disabled={loading}>
              <RefreshCw className={`mr-2 size-4 ${loading ? "animate-spin" : ""}`} /> 刷新
            </Button>
            <Button onClick={() => setCreateOpen(true)}>
              <Plus className="mr-2 size-4" /> 创建 Key
            </Button>
          </div>
        </div>
      </section>

      <div className="grid gap-4 md:grid-cols-3">
        <div className="rounded-[28px] border border-white/75 bg-card/82 p-5 shadow-[var(--shadow-soft)]">
          <div className="text-sm text-muted-foreground">下游 Key 数量</div>
          <div className="mt-3 text-4xl font-black">{keys.length}</div>
        </div>
        <div className="rounded-[28px] border border-white/75 bg-card/82 p-5 shadow-[var(--shadow-soft)]">
          <div className="text-sm text-muted-foreground">最新 Key</div>
          <div className="mt-3 truncate font-mono text-xl font-black">{latestKey ? maskKey(latestKey) : "未生成"}</div>
        </div>
        <div className="rounded-[28px] border border-white/75 bg-card/82 p-5 shadow-[var(--shadow-soft)]">
          <div className="text-sm text-muted-foreground">认证方式</div>
          <div className="mt-3 inline-flex items-center gap-2 rounded-full border bg-accent/70 px-3 py-1 text-sm font-bold text-accent-foreground">
            <ShieldCheck className="size-4" />
            Bearer / x-api-key
          </div>
        </div>
      </div>

      <section className="overflow-hidden rounded-[30px] border border-white/75 bg-card/86 shadow-[var(--shadow-lift)]">
        <div className="flex items-center justify-between border-b border-border/50 bg-muted/10 px-6 py-5">
          <div>
            <h3 className="text-xl font-black tracking-tight">Key 列表</h3>
            <p className="text-sm text-muted-foreground">Key 默认遮蔽展示，复制时会写入完整值；环境变量注入 Key 需要从环境变量移除。</p>
          </div>
          <KeyRound className="size-8 text-muted-foreground/30" />
        </div>
        <div className="divide-y divide-border/50">
          {keys.length === 0 ? (
            <div className="grid min-h-72 place-items-center p-8 text-center text-muted-foreground">
              <div>
                <KeyRound className="mx-auto mb-4 size-12 opacity-30" />
                <div className="font-semibold text-foreground">暂无 API Key</div>
                <p className="mt-1 text-sm">点击“创建 Key”创建下游访问凭证，或通过环境变量注入。</p>
              </div>
            </div>
          ) : (
            keys.map((item, index) => (
              <div key={item.key} className="grid gap-4 px-6 py-5 lg:grid-cols-[auto_1fr_auto] lg:items-center">
                <div className="grid size-10 place-items-center rounded-2xl bg-muted font-mono text-sm font-black">{index + 1}</div>
                <div className="min-w-0">
                  <div className="truncate font-mono text-sm font-bold">{maskKey(item.key)}</div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    <span>完整 Key 不直接明文展示，避免旁观泄露。</span>
                    <span className={`rounded-full border px-2 py-0.5 font-bold ${item.source === "env" ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "bg-muted text-muted-foreground"}`}>
                      {item.label || (item.source === "env" ? "环境变量注入 Key" : "面板创建 Key")}
                    </span>
                  </div>
                </div>
                <div className="flex justify-end gap-2">
                  <Button variant="secondary" size="sm" onClick={() => void copyToClipboard(item.key)}>
                    {copied === item.key ? <Check className="mr-2 size-4 text-emerald-600" /> : <Copy className="mr-2 size-4" />}
                    复制
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDelete(item)}
                    disabled={item.source === "env"}
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                    title={item.source === "env" ? "环境变量 Key 需要从环境变量中移除" : "删除"}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              </div>
            ))
          )}
        </div>
      </section>

      {createOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-[28px] border border-white/75 bg-card p-5 shadow-[var(--shadow-lift)]">
            <div className="flex items-center justify-between border-b border-border/50 pb-4">
              <div className="flex items-center gap-2">
                <KeyRound className="size-5 text-primary" />
                <h3 className="text-lg font-black">创建 API Key</h3>
              </div>
              <Button variant="ghost" size="icon" onClick={() => setCreateOpen(false)} title="关闭">
                <X className="size-4" />
              </Button>
            </div>
            <div className="space-y-4 pt-4">
              <div className="grid grid-cols-2 rounded-2xl border bg-muted/30 p-1">
                <button
                  type="button"
                  onClick={() => setCreateMode("auto")}
                  className={`rounded-xl px-3 py-2 text-sm font-bold ${createMode === "auto" ? "bg-background shadow-sm" : "text-muted-foreground"}`}
                >
                  自动生成
                </button>
                <button
                  type="button"
                  onClick={() => setCreateMode("custom")}
                  className={`rounded-xl px-3 py-2 text-sm font-bold ${createMode === "custom" ? "bg-background shadow-sm" : "text-muted-foreground"}`}
                >
                  自定义 Key
                </button>
              </div>

              {createMode === "custom" && (
                <div className="space-y-2">
                  <label className="text-sm font-bold">API Key</label>
                  <input
                    type="text"
                    value={customKey}
                    onChange={e => setCustomKey(e.target.value)}
                    placeholder="sk-your-custom-key"
                    className="flex h-11 w-full rounded-2xl border border-input bg-background px-3 py-2 font-mono text-sm"
                  />
                </div>
              )}

              <div className="flex justify-end gap-2 pt-2">
                <Button variant="outline" onClick={() => setCreateOpen(false)}>取消</Button>
                <Button onClick={handleCreate}>
                  <Plus className="mr-2 size-4" /> 创建
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
