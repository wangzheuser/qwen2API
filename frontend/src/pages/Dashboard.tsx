import { useEffect, useState } from "react"
import { Server, Activity, ShieldAlert, ActivityIcon, FileJson, Cpu, Shield, Globe, ImageIcon, Paperclip, Flame, Database } from "lucide-react"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import { toast } from "sonner"

type Status = {
  accounts?: {
    total?: number
    valid?: number
    rate_limited?: number
    invalid?: number
    in_use?: number
    global_in_use?: number
    global_max_inflight?: number
    waiting?: number
    max_inflight_per_account?: number
    max_queue_size?: number
  }
  chat_id_pool?: {
    total_cached?: number
    target_per_account?: number
    configured_target_per_account?: number
    ttl_seconds?: number
    large_pool_suppressed?: boolean
  } | null
  browser_automation?: {
    metrics?: {
      limit?: number
      active?: number
      waiting?: number
      launched_total?: number
      failed_total?: number
    }
  }
  upstream_proxy?: {
    enabled?: boolean
    template_mode?: boolean
    bound_accounts?: number
    failures_total?: number
  }
  runtime?: { asyncio_running_tasks?: number }
}

export default function Dashboard() {
  const [status, setStatus] = useState<Status | null>(null)
  const [errOnce, setErrOnce] = useState(false)

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/admin/status`, { headers: getAuthHeader() })
        if (!res.ok) throw new Error("Unauthorized")
        const data = await res.json()
        setStatus(data)
      } catch {
        if (!errOnce) {
          toast.error("状态获取失败，请在「系统设置」检查您的当前会话 Key。")
          setErrOnce(true)
        }
      }
    }
    fetchStatus()
    const timer = setInterval(fetchStatus, 3000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const acc = status?.accounts || {}
  const pool = status?.chat_id_pool
  const browser = status?.browser_automation?.metrics || {}
  const proxy = status?.upstream_proxy || {}

  return (
    <div className="space-y-8 max-w-5xl relative">
      <div className="relative z-10">
        <div className="absolute -top-10 -left-10 w-40 h-40 bg-primary/20 blur-[100px] rounded-full pointer-events-none" />
        <h2 className="text-3xl font-extrabold tracking-tight bg-gradient-to-r from-foreground to-foreground/60 bg-clip-text text-transparent">运行状态</h2>
        <p className="text-muted-foreground mt-2 text-lg">全局并发监控与千问账号池概览（每 3 秒自动刷新）。</p>
      </div>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4 relative z-10">
        <StatCard icon={<Server className="h-5 w-5 text-primary" />} title="可用账号" value={String(acc.valid ?? 0)} accent="primary" sub={`共 ${acc.total ?? 0} 个`} />
        <StatCard icon={<Activity className="h-5 w-5 text-blue-400" />} title="当前并发" value={String(acc.in_use ?? 0)} accent="blue" sub={`全局 ${acc.global_in_use ?? 0} / ${acc.global_max_inflight ?? 0}`} />
        <StatCard icon={<ShieldAlert className="h-5 w-5 text-destructive" />} title="排队请求" value={String(acc.waiting ?? 0)} accent="destructive" sub={`队列上限 ${acc.max_queue_size ?? 0}`} />
        <StatCard icon={<ActivityIcon className="h-5 w-5 text-orange-400" />} title="限流号/失效号" value={`${acc.rate_limited ?? 0} / ${acc.invalid ?? 0}`} accent="orange" />
      </div>

      <div className="grid gap-6 md:grid-cols-3 relative z-10">
        <StatCard icon={<Flame className="h-5 w-5 text-rose-400" />} title="Chat_ID 预热池" value={String(pool?.total_cached ?? 0)} accent="rose" sub={pool ? `实际目标 ${pool.target_per_account} / 配置 ${pool.configured_target_per_account ?? pool.target_per_account} · ${pool.large_pool_suppressed ? "大池已暂停" : `TTL ${Math.round((pool.ttl_seconds || 0) / 60)} 分钟`}` : "未启用"} />
        <StatCard icon={<Database className="h-5 w-5 text-cyan-400" />} title="浏览器实例" value={String(browser.active ?? 0)} accent="cyan" sub={`等待 ${browser.waiting ?? 0} · 上限 ${browser.limit ?? 0} · 失败 ${browser.failed_total ?? 0}`} />
        <StatCard icon={<Globe className="h-5 w-5 text-blue-400" />} title="上游代理" value={proxy.enabled ? "ON" : "OFF"} accent="blue" sub={`绑定 ${proxy.bound_accounts ?? 0} · 失败 ${proxy.failures_total ?? 0} · ${proxy.template_mode ? "UUID池" : "静态"}`} />
      </div>

      <div className="rounded-2xl border border-border/50 bg-card/30 backdrop-blur-xl shadow-2xl relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-b from-black/[0.02] dark:from-white/[0.02] to-transparent pointer-events-none" />
        <div className="flex flex-col space-y-2 p-8 border-b border-border/50 bg-muted/10 relative z-10">
          <h3 className="font-extrabold text-2xl tracking-tight flex items-center gap-3">
            <span className="bg-primary w-2 h-8 rounded-full shadow-[0_0_10px_rgba(168,85,247,0.5)]"></span>
            API 接口池
          </h3>
          <p className="text-base text-muted-foreground ml-5">兼容主流 AI 协议的调用入口，默认无需认证，或通过 API Key 访问。</p>
        </div>
        <div className="p-0 relative z-10">
          <div className="divide-y divide-border/50 text-sm">
            <EndpointRow icon={<FileJson className="h-5 w-5 text-emerald-500 dark:text-emerald-400" />} iconBg="bg-emerald-500/10" path="POST /v1/chat/completions" tag="OpenAI" tagColor="emerald" />
            <EndpointRow icon={<Cpu className="h-5 w-5 text-blue-500 dark:text-blue-400" />} iconBg="bg-blue-500/10" path="POST /v1/messages" tag="Anthropic" tagColor="blue" />
            <EndpointRow icon={<Globe className="h-5 w-5 text-yellow-600 dark:text-yellow-400" />} iconBg="bg-yellow-500/10" path="POST /v1/models/gemini-pro:generateContent" tag="Gemini" tagColor="yellow" />
            <EndpointRow icon={<ImageIcon className="h-5 w-5 text-purple-500 dark:text-purple-400" />} iconBg="bg-purple-500/10" path="POST /v1/images/generations" tag="Image Gen" tagColor="purple" />
            <EndpointRow icon={<Paperclip className="h-5 w-5 text-cyan-500 dark:text-cyan-400" />} iconBg="bg-cyan-500/10" path="POST /v1/files" tag="Files" tagColor="cyan" />
            <EndpointRow icon={<Shield className="h-5 w-5 text-slate-600 dark:text-slate-400" />} iconBg="bg-slate-500/10" path="GET /" tag="健康检查" tagColor="slate" />
          </div>
        </div>
      </div>
    </div>
  )
}

function StatCard({ icon, title, value, accent, sub }: { icon: React.ReactNode; title: string; value: string; accent: string; sub?: string }) {
  const shadowMap: Record<string, string> = {
    primary: "hover:shadow-primary/5",
    blue: "hover:shadow-blue-500/5",
    destructive: "hover:shadow-destructive/10",
    orange: "hover:shadow-orange-500/5",
    rose: "hover:shadow-rose-500/5",
    cyan: "hover:shadow-cyan-500/5",
  }
  const gradMap: Record<string, string> = {
    primary: "from-primary/10",
    blue: "from-blue-500/10",
    destructive: "from-destructive/10",
    orange: "from-orange-500/10",
    rose: "from-rose-500/10",
    cyan: "from-cyan-500/10",
  }
  return (
    <div className={`group rounded-2xl border border-border/50 bg-card/40 backdrop-blur-md shadow-xl ${shadowMap[accent]} transition-all duration-500 overflow-hidden relative`}>
      <div className={`absolute inset-0 bg-gradient-to-br ${gradMap[accent]} to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500`} />
      <div className="p-6 relative z-10">
        <div className="flex flex-row items-center justify-between space-y-0 pb-4">
          <h3 className="tracking-tight text-sm font-semibold text-foreground/80 uppercase">{title}</h3>
          <div className="p-2 bg-primary/10 rounded-lg">{icon}</div>
        </div>
        <div className="text-4xl font-black bg-gradient-to-r from-foreground to-foreground/70 bg-clip-text text-transparent">
          {value}
        </div>
        {sub ? <div className="text-xs text-muted-foreground mt-2">{sub}</div> : null}
      </div>
    </div>
  )
}

function EndpointRow({ icon, iconBg, path, tag, tagColor }: { icon: React.ReactNode; iconBg: string; path: string; tag: string; tagColor: string }) {
  const tagClass = `bg-${tagColor}-500/10 text-${tagColor}-600 dark:bg-${tagColor}-500/20 dark:text-${tagColor}-300 ring-1 ring-${tagColor}-500/20 dark:ring-${tagColor}-500/30`
  return (
    <div className="flex justify-between items-center px-8 py-5 hover:bg-black/5 dark:hover:bg-white/[0.02] transition-colors">
      <div className="flex items-center gap-4">
        <div className={`p-2 rounded-md ${iconBg}`}>{icon}</div>
        <div className="font-semibold text-foreground/80">{path}</div>
      </div>
      <span className={`inline-flex items-center rounded-full px-3 py-1 text-xs font-bold ${tagClass}`}>{tag}</span>
    </div>
  )
}
