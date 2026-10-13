"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTheme } from "next-themes";
import { z } from "zod";
import { ArrowUpRight, FolderTree, Save, ShieldCheck, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { FactoryGuard, Field, PageHeading, Panel } from "@/components/factory-ui";
import { useNotice } from "@/components/providers";
import { API_URL, request, useFactory, useAppMode } from "@/lib/api";
import { ProductionReadinessPanel } from "@/components/production-ui";
import type { Settings,ProductionPolicy } from "@/lib/types";

const settingsSchema = z.object({
  output_width: z.number().int().positive("输出宽度必须为正整数"),
  output_height: z.number().int().positive("输出高度必须为正整数"),
  output_fps: z.number().positive("FPS 必须大于 0"),
  mock_delay_ms: z.number().int().min(100, "模拟间隔至少 100 ms").max(5000, "模拟间隔最多 5000 ms"),
});
const folders = ["source", "frames", "cutout", "person", "background", "compose", "final", "workflow", "metadata", "logs"];

export default function SettingsPage() {
  const factory = useFactory();
  const mode = useAppMode();
  const { setTheme } = useTheme();
  const notice = useNotice();
  const [draft, setDraft] = useState<Settings | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [saving,setSaving]=useState(false);
  const production=useQuery({queryKey:["production-settings"],queryFn:()=>request<{settings:Settings;effective_policy:ProductionPolicy;quality_thresholds:Record<string,number>;storage_root:string}>("/api/settings/production"),enabled:mode.data?.app_mode==="integration",retry:false});
  const settings = draft ?? factory.data?.settings;
  const policy=production.data?{...production.data.effective_policy,...settings?.production_policy}:undefined;
  const thresholds={...production.data?.quality_thresholds,...policy?.quality_thresholds};

  function update(patch: Partial<Settings>) {
    if (settings) setDraft({ ...settings, ...patch });
  }
  function updatePolicy(patch:Partial<ProductionPolicy>){if(settings&&policy)setDraft({...settings,production_policy:{...policy,...patch}});}
  async function save() {
    if (!settings) return;
    const result = settingsSchema.safeParse(settings);
    if (!result.success) { notice(result.error.issues[0].message); return; }
    setSaving(true);try{if(mode.data?.app_mode==="integration"){if(!policy)throw new Error("生产策略尚未读取，未保存");await request("/api/settings/production",{method:"PUT",body:JSON.stringify({...settings,production_policy:policy})});await production.refetch();await factory.refresh();notice("Factory 生产策略已保存；后续调度使用，不修改旧规则");}else if(!await factory.act("save_settings",undefined,settings)){return;}setTheme(settings.theme);setDraft(null);}catch(error){notice(error instanceof Error?error.message:"设置保存失败");}finally{setSaving(false);}
  }

  return <>
    <PageHeading eyebrow="WORKSPACE PREFERENCES" title="设置" description="真实保存 Factory 生产策略；旧项目规则、环境和存储不被改写。">
      <Button disabled={saving || factory.working || !settings || mode.data?.app_mode==="integration"&&!policy} onClick={save}><Save size={14} />{saving?"正在保存":"保存设置"}</Button>
    </PageHeading>
    <FactoryGuard factory={factory}>{settings && <div className="grid items-start gap-7 xl:grid-cols-[minmax(0,1fr)_350px]">
      <div className="space-y-7">
        <Panel title="界面与输出" description="真实合成沿用当前工作流：按原片比例缩放、保留原始 FPS；此页不改写旧工作流。">
          <div className="grid gap-6 sm:grid-cols-2">
            <Field label="外观"><select className="h-9 rounded-lg border bg-background px-3 text-xs" value={settings.theme} onChange={e => update({ theme: e.target.value })}><option value="light">浅色</option><option value="dark">深色</option><option value="system">跟随系统</option></select></Field>
            <Field label="语言" hint="第一阶段只提供简体中文"><Input readOnly value="简体中文 · zh-CN" /></Field>
            <Field label="输出宽高" hint="尺寸取决于原片比例和已配置的短边，实际 ffprobe 在资产详情显示"><Input readOnly value="按原片比例缩放 · 工作流固定"/></Field>
            <Field label="输出 FPS" hint="母资产与合成保留来源视频的原始帧率"><Input readOnly value="保留 SOURCE FPS"/></Field>
            {policy&&<Field label="背景生产方式"><select aria-label="背景生产方式" className="h-9 rounded-lg border bg-background px-3 text-xs" value={policy.background_strategy} onChange={event=>updatePolicy({background_strategy:event.target.value as ProductionPolicy["background_strategy"]})}><option value="chatgpt_rpa">现有 ChatGPT / 影刀流程</option><option value="manual">等待手动上传真实背景</option></select></Field>}
          </div>
        </Panel>
        {mode.data?.app_mode==="integration"?<Panel title="生产策略" description="后端实际读取这些配置；首次成片最终人工审核始终保留。">{production.isPending?<p className="text-xs">正在读取有效策略…</p>:production.isError?<p role="alert" className="text-xs text-red-600">{production.error.message}</p>:policy&&<><div className="grid gap-6 sm:grid-cols-2"><Field label="默认并发" hint="现有浏览器与外部服务共享环境，当前真实队列只允许串行"><Input aria-label="默认并发" type="number" value={policy.concurrency} readOnly/></Field><Field label="本地 / 网络失败自动重试次数" hint="状态未知的付费请求不自动重交"><Input aria-label="失败重试次数" type="number" min={0} max={5} step={1} value={policy.retry_limit} onChange={event=>updatePolicy({retry_limit:Number(event.target.value)})}/></Field><label className="flex items-start gap-3 text-xs leading-6"><input aria-label="自动进入下一阶段" type="checkbox" className="mt-1" checked={policy.auto_advance} onChange={event=>updatePolicy({auto_advance:event.target.checked})}/><span>自动进入下一阶段<br/><span className="text-[10px] text-muted-foreground">前置条件满足后自动推进；关闭时按你的选择等待继续。</span></span></label><label className="flex items-start gap-3 text-xs leading-6"><input aria-label="重启后安全续跑" type="checkbox" className="mt-1" checked={policy.auto_resume} onChange={event=>updatePolicy({auto_resume:event.target.checked})}/><span>重启后安全续跑<br/><span className="text-[10px] text-muted-foreground">核实外部请求和已保存资产，不重复未知付费请求。</span></span></label><label className="flex items-start gap-3 text-xs leading-6"><input aria-label="人像质量通过后仍需审核" type="checkbox" className="mt-1" checked={policy.person_review_required} onChange={event=>updatePolicy({person_review_required:event.target.checked})}/><span>人像机器质量通过后仍需人工审核<br/><span className="text-[10px] text-muted-foreground">质量不通过或语义无法判断时始终保留人工介入。</span></span></label></div><p className="mt-5 border-t pt-4 text-[10px] leading-6 text-muted-foreground">这些策略保存至 Factory 数据库；已运行步骤的输入指纹与结果保留。原片审核、困难区域修复及首条成片验收不会被自动批准。</p></>}</Panel>:null}
        {mode.data?.app_mode==="integration"&&policy&&<Panel title="机器几何质量阈值" description="只影响新的质量分析配置指纹；几何分数不能证明人物、产品或语义完整。"><div className="grid gap-5 sm:grid-cols-2">{[["max_area_change","最大相邻帧面积变化"],["max_hole_ratio","最大内部空洞比例"],["min_motion_iou","最低运动对齐稳定度"],["max_bbox_jump","最大主体边框跳动"],["min_keep_object_coverage","KEEP 物体最低覆盖率"]].map(([key,label])=><Field key={key} label={label}><Input aria-label={label} type="number" min={0} max={1} step={0.01} value={thresholds[key]??""} onChange={event=>updatePolicy({quality_thresholds:{...policy.quality_thresholds,[key]:Number(event.target.value)}})}/></Field>)}</div><p className="mt-5 text-[10px] leading-6 text-muted-foreground">改变阈值不会覆盖旧质量报告，也不会自动把历史 FAIL 改成 PASS。任务在下一次真实质检时消费新配置。</p></Panel>}
        {mode.data?.app_mode==="mock"&&<Panel title="独立 Mock 工作流" description="只用于独立演示模式，不进入真实生产链路。">
          <div className="grid gap-6 sm:grid-cols-2">
            <Field label="模拟步骤间隔（ms）"><Input type="number" min={100} max={5000} value={settings.mock_delay_ms} onChange={e => update({ mock_delay_ms: Number(e.target.value) })} /></Field>
            <Field label="成片审核"><select className="h-9 rounded-lg border bg-background px-3 text-xs" value={settings.review_required ? "yes" : "no"} onChange={e => update({ review_required: e.target.value === "yes" })}><option value="yes">合成后进入审核</option><option value="no">Mock 自动通过并记录审计</option></select></Field>
          </div>
          <p className="mt-5 text-[11px] leading-6 text-muted-foreground">当前模式：{mode.data?.app_mode || "UNKNOWN"}。这里的模拟设置不改变真实生产规则。TikTok Publishing 始终为 MOCK。</p>
          <div className="mt-6 flex flex-wrap items-center justify-between gap-4 border-t pt-5"><div><p className="text-xs font-medium">恢复示例数据</p><p className="mt-2 text-[10px] text-muted-foreground">真实生产模式禁止重置，以保留任务与失败历史。</p></div><Button variant="outline" disabled={mode.data?.app_mode!=="mock"} title="仅独立 Mock 模式可用" onClick={() => setResetOpen(true)}><RotateCcw size={14} />重置 Mock 数据</Button></div>
        </Panel>}
        <Panel title="密钥与授权边界" action={<ShieldCheck size={17} />}>
          <p className="text-xs leading-7">真实 Provider 读取现有项目的环境配置。此界面不收集 API Key、密码、Cookie 或 Access Token；账号只保存 Secret Store / Environment Variable 的引用。</p>
          <p className="mt-4 border-t pt-4 font-mono text-[10px] text-muted-foreground">PublishingAccount → secret_ref → Environment / Secret Store</p>
        </Panel>
      </div>
      <aside className="space-y-7">
        <Panel title="系统连接"><dl className="space-y-5 text-xs"><div><dt className="text-muted-foreground">Backend API</dt><dd className="mt-2 break-all font-mono text-[11px]">{API_URL}</dd></div><div><dt className="text-muted-foreground">数据库</dt><dd className="mt-2">本地 SQLite · PostgreSQL 可迁移模型</dd></div><div><dt className="text-muted-foreground">Publishing Backend</dt><dd className="mt-2">{mode.isError?"读取失败":mode.data?.publishing_backend||"UNKNOWN"}</dd></div><div><dt className="text-muted-foreground">Publishing Readiness</dt><dd className="mt-2">{mode.isError?"读取失败":mode.data?.publishing_production_readiness||"UNKNOWN"}</dd></div><div><dt className="text-muted-foreground">调度</dt><dd className="mt-2">Factory 串行队列 · 独立 Worker 未连接</dd></div></dl><a href={`${API_URL}/docs`} target="_blank" rel="noreferrer" className="mt-6 inline-flex items-center gap-2 text-xs">查看 API 文档<ArrowUpRight size={13} /></a><p className="mt-4 text-[10px] leading-5 text-muted-foreground">后端地址在 frontend/.env.local 的 NEXT_PUBLIC_API_URL 修改。</p></Panel>
        <Panel title="任务存储结构" description="按任务与版本保存，旧系统文件不迁移。" action={<FolderTree size={16} />}>
          <p className="mb-4 break-all font-mono text-[10px] text-muted-foreground">{production.data?.storage_root||"正在读取当前存储位置…"}</p><pre className="whitespace-pre-wrap break-all font-mono text-[10px] leading-6 text-muted-foreground">{`storage/projects/{project_id}/tasks/{task_id}/\n${folders.map((name, index) => `${index === folders.length - 1 ? "└" : "├"}── ${name}/`).join("\n")}`}</pre>
        </Panel>
      </aside>
    </div>}<div className="mt-7"><ProductionReadinessPanel/></div></FactoryGuard>
    <Dialog open={resetOpen} onOpenChange={setResetOpen}><DialogContent><DialogHeader><DialogTitle>恢复本地 Mock 示例？</DialogTitle><DialogDescription>会清除这个原型中新建的任务和编辑记录，恢复演示数据。不会读取或修改 TikTok_Auto、video_auto 或 ComfyUI。</DialogDescription></DialogHeader><div className="flex justify-end gap-2"><Button variant="outline" onClick={() => setResetOpen(false)}>取消</Button><Button disabled={factory.working} onClick={async () => { if (await factory.act("reset_demo")) { setDraft(null); setResetOpen(false); setTheme("light"); } }}>确认重置</Button></div></DialogContent></Dialog>
  </>;
}
