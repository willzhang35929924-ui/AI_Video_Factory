"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useTheme } from "next-themes";
import { useCallback, useEffect, useRef, useState } from "react";
import { Aperture, LayoutDashboard, Radar, Film, Scan, ImageIcon, Layers, CheckCheck, Send, Users, Package, Settings, Search, Sun, Moon, Menu, X, ChevronRight } from "lucide-react";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { API_URL, useProductionReadiness, useProductionQueue } from "@/lib/api";
export const navigation = [
  { href: "/dashboard", label: "总览", icon: LayoutDashboard }, { href: "/collector", label: "采集", icon: Radar },
  { href: "/assets", label: "素材", icon: Film }, { href: "/cutout", label: "抠像", icon: Scan },
  { href: "/background", label: "背景", icon: ImageIcon }, { href: "/compose", label: "合成", icon: Layers },
  { href: "/review", label: "审核", icon: CheckCheck }, { href: "/publish", label: "发布", icon: Send },
  { href: "/accounts", label: "账号", icon: Users }, { href: "/products", label: "产品", icon: Package },
  { href: "/settings", label: "设置", icon: Settings },
];
export function AppShell({ children }: { children: React.ReactNode }) {
  const readiness = useProductionReadiness(); const queue = useProductionQueue();
  const path = usePathname(); const { resolvedTheme, setTheme } = useTheme();
  const [mobile, setMobile] = useState(false); const [search, setSearch] = useState<string | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const searchTrigger = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const openSearch = useCallback(() => {
    if (search !== null) {
      searchInput.current?.focus();
      return;
    }
    returnFocus.current = document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : searchTrigger.current;
    setSearch("");
  }, [search]);
  const searchResults = navigation.filter(item => item.label.includes(search?.trim() || ""));
  const health = useQuery({ queryKey: ["health"], queryFn: async () => { const response = await fetch(`${API_URL}/api/health`, { signal: AbortSignal.timeout(5000) }); if (!response.ok) throw new Error("API 未连接"); return response.json(); }, refetchInterval: 10000 });
  useEffect(() => { const key = (event: KeyboardEvent) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openSearch(); } if (event.key === "Escape") { setMobile(false); } }; window.addEventListener("keydown", key); return () => window.removeEventListener("keydown", key); }, [openSearch]);
  const label = path.startsWith("/tasks/") ? "任务详情" : navigation.find(item => path.startsWith(item.href))?.label || "总览";
  return <div className="min-h-screen bg-background text-foreground">
    {mobile && <button aria-label="关闭导航遮罩" className="fixed inset-0 z-30 bg-black/20 lg:hidden" onClick={() => setMobile(false)} />}
    <aside className={`fixed inset-y-0 left-0 z-40 flex w-60 flex-col overflow-y-auto border-r bg-sidebar px-4 py-7 transition-transform lg:translate-x-0 ${mobile ? "translate-x-0 visible" : "-translate-x-full invisible lg:visible"}`}>
      <Link href="/dashboard" className="mb-10 flex items-center gap-3 px-3" onClick={() => setMobile(false)}><span className="flex size-8 items-center justify-center rounded-lg bg-foreground text-background"><Aperture size={20} /></span><span className="text-[14px] font-semibold tracking-tight">AI Video Factory</span></Link>
      <p className="mb-3 px-3 text-[10px] font-medium tracking-[.18em] text-muted-foreground">生产工作空间</p>
      <nav aria-label="主导航" className="space-y-1">{navigation.map((item, index) => <div key={item.href} className={index === 8 || index === 10 ? "mt-5 border-t pt-5" : ""}><Link href={item.href} onClick={() => setMobile(false)} aria-current={path.startsWith(item.href) ? "page" : undefined} className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-[13px] transition-colors ${path.startsWith(item.href) ? "bg-sidebar-accent font-semibold text-foreground" : "text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"}`}><item.icon size={17} strokeWidth={1.6} />{item.label}</Link></div>)}</nav>
      <div className="mt-auto border-t px-3 pt-5 text-[11px] text-muted-foreground"><p className="mb-3 font-medium text-foreground">系统状态 <span className="float-right rounded bg-muted px-1.5 text-[9px]">{health.data?.app_mode==="integration"?"REAL":health.data?.app_mode==="mock"?"MOCK":"UNKNOWN"}</span></p>{[["API", health.isError ? "未连接" : health.isPending ? "连接中" : "在线"], ["ComfyUI", readiness.data?.checks.find(check=>check.name.toLowerCase().includes("comfy"))?.status || "UNKNOWN"], ["生产队列", queue.data?.status || "UNKNOWN"], ["独立 Worker", "未连接"]].map(([name, value]) => <div key={name} className="mb-2 flex items-center justify-between gap-2"><span>{name}</span><span className="flex items-center gap-1.5 text-[10px]"><span className={`size-1.5 rounded-full ${name === "API" && !health.isError && !health.isPending ? "bg-emerald-500" : "bg-neutral-400"}`} />{value}</span></div>)}</div>
    </aside>
    <div className="lg:pl-60"><header className="flex h-18 items-center justify-between border-b px-5 sm:px-9"><div className="flex items-center gap-3 text-xs text-muted-foreground"><button className="lg:hidden" aria-label="打开导航" onClick={() => setMobile(true)}><Menu size={20} /></button><span>默认项目</span><ChevronRight size={13} /><span className="text-foreground">{label}</span></div><div className="flex items-center gap-4"><button ref={searchTrigger} aria-label="快速查找" onClick={openSearch} className="flex items-center gap-2 text-xs text-muted-foreground"><Search size={15} /><span className="hidden sm:inline">快速查找</span><kbd className="hidden rounded border px-1 sm:inline">⌘ K</kbd></button><button aria-label="切换明暗主题" onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")} className="rounded-md p-2 hover:bg-muted"><Sun size={16} className="dark:hidden" /><Moon size={16} className="hidden dark:block" /></button><span className="flex size-8 items-center justify-center rounded-full bg-muted text-xs">本地</span></div></header><main className="mx-auto max-w-[1500px] px-5 py-9 sm:px-9 lg:px-12 lg:py-11">{children}</main></div>
    <Dialog open={search !== null} onOpenChange={open => { if (!open) setSearch(null); }}>
      <DialogContent
        showCloseButton={false}
        initialFocus={searchInput}
        finalFocus={() => returnFocus.current?.isConnected ? returnFocus.current : searchTrigger.current}
        className="top-[15vh] w-[calc(100%-3rem)] max-w-lg translate-y-0 gap-0 rounded-xl border bg-background p-4 ring-0 sm:w-full sm:max-w-lg"
      >
        <DialogTitle className="sr-only">快速查找页面</DialogTitle>
        <DialogDescription className="sr-only">输入页面名称查找工作台。按 Tab 选择结果，按 Escape 关闭查找。</DialogDescription>
        <div className="flex items-center gap-3 border-b pb-3">
          <Search size={18} />
          <input ref={searchInput} aria-label="查找页面" value={search || ""} onChange={event => setSearch(event.target.value)} className="w-full bg-transparent outline-none" placeholder="查找工作台…" />
          <DialogClose aria-label="关闭查找" className="rounded p-1 focus-visible:outline-2 focus-visible:outline-ring"><X size={16} /></DialogClose>
        </div>
        <div className="mt-3 grid max-h-[60vh] gap-1 overflow-y-auto">
          {searchResults.length ? searchResults.map(item => (
            <Link key={item.href} href={item.href} onClick={() => setSearch(null)} className="flex items-center gap-3 rounded-lg p-3 text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring">
              <item.icon size={16} />{item.label}<ChevronRight size={14} className="ml-auto" />
            </Link>
          )) : <p role="status" className="px-3 py-6 text-center text-xs text-muted-foreground">没有匹配的页面，请尝试其他名称。</p>}
        </div>
      </DialogContent>
    </Dialog>
  </div>;
}
