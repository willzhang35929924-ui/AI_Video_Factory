"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, Copy, Play, Upload } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, Panel, ProgressBar, StatusBadge } from "@/components/factory-ui";
import { AssetPreview, RunLogDrawer } from "@/components/production-ui";
import { useNotice } from "@/components/providers";
import { request, useFactory, useProductionQueue, useProductionReadiness } from "@/lib/api";
import { durationLabel, type Asset, type IssueSegment, type Prompt, type Task } from "@/lib/types";
import { display } from "@/components/integration-collector";

function issueTime(value:number) {
  const milliseconds=Math.round(value*1000);
  return `${String(Math.floor(milliseconds/60000)).padStart(2,"0")}:${String(Math.floor(milliseconds%60000/1000)).padStart(2,"0")}.${String(milliseconds%1000).padStart(3,"0")}`;
}

type ProductionContext = {
  current_person_asset_id?:string;
  requested_stage?:string;
  requested_prompt_id?:string;
  active_prompt_id?:string;
  active_stage?:string;
  active_run_id?:string;
  waiting?:{reason:string;stage:string;message:string;resume?:string};
  results?:Record<string,{status?:string;run_id?:string;error_code?:string;error_message?:string}>;
  first_composition?:{status?:string;human_review?:string;person_asset_id?:string;composition_asset_id?:string};
};

function AutomaticProductionControl({task,factory}:{task:Task;factory:ReturnType<typeof useFactory>}) {
  const queue=useProductionQueue();
  const notice=useNotice();
  const [resumingImport,setResumingImport]=useState(false);
  const production=task.context.production as ProductionContext | undefined;
  const runs=factory.data?.runs.filter(run=>run.task_id===task.id) || [];
  const active=runs.find(run=>["queued","running"].includes(run.status));
  const queued=queue.data?.items?.find(item=>item.task_id===task.id && ["QUEUED","RUNNING"].includes(item.status));
  const paused=queue.data?.status==="PAUSED" && queue.data.task_ids?.includes(task.id);
  const unknown=runs.some(run=>run.error_code==="UNKNOWN_EXTERNAL_REQUEST");
  const waiting=production?.waiting;
  const localCutout=!!task.context.local_cutout;
  const localSourcePending=localCutout && !(task.context.local_cutout as {full_decode_verified?:boolean}).full_decode_verified;
  const reviewGate=!!waiting && ["PERSON_QUALITY_REVIEW_REQUIRED","FIRST_COMPOSITION_REVIEW_REQUIRED","SOURCE_WATERMARK_REVIEW_REQUIRED","LOCAL_PERSON_REVIEW_REQUIRED"].includes(waiting.reason);
  const blocked=task.status==="REJECTED" || unknown || reviewGate;
  const working=resumingImport || factory.working || !!active || !!queued && !paused;
  const status=paused?"PAUSED":active?"RUNNING":queued?"QUEUED":waiting?"WAITING_MANUAL":task.status;
  function continueProduction() {
    if(localSourcePending) {
      setResumingImport(true);
      void request(`/api/local-cutout/tasks/${encodeURIComponent(task.id)}/resume-import`,{method:"POST"})
        .then(()=>factory.refresh()).catch(error=>notice(error instanceof Error?error.message:"本地导入恢复失败"))
        .finally(()=>setResumingImport(false));
      return;
    }
    if(paused)return factory.act("production.resume",task.id,{batch_id:queue.data?.batch_id});
    if(task.status==="FAILED")return factory.act("production.retry",task.id,{task_ids:[task.id],...(localCutout?{stage:"cutout"}:{})});
    return factory.act(production?"production.continue":"production.start",task.id,{task_ids:[task.id]});
  }
  return <div className="rounded-lg border p-4" aria-label="自动生产进度">
    <div className="flex flex-wrap items-center justify-between gap-3"><p className="text-xs font-medium">自动生产线</p><StatusBadge status={status}/></div>
    <p className="mt-3 text-[11px] leading-6 text-muted-foreground">{waiting?.message || (task.status==="REJECTED"?"当前任务已拒绝，后续生产已停止。":active?`正在执行 ${active.stage}，完成后自动进入下一阶段。`:queued?"已进入串行生产队列，无需逐阶段点击。":"一次开始，自动复用已完成的采集、下载、RGB / Alpha、提示词与背景。")}</p>
    {waiting&&<><p className="mt-2 break-words font-mono text-[9px] text-muted-foreground">{waiting.reason} · 下一步 {waiting.stage}</p><p className="mt-2 text-[10px] leading-5 text-muted-foreground">{waiting.resume || "前置条件处理后继续此任务；已有核验资产保留。"}</p></>}
    {unknown&&<p role="alert" className="mt-3 text-[10px] text-red-600">UNKNOWN_EXTERNAL_REQUEST：先确认外部请求是否完成，不能自动重复计费。</p>}
    <div className="mt-4 flex flex-wrap gap-2"><Button disabled={working || blocked} onClick={continueProduction}><Play size={13}/>{paused?"恢复生产队列":working?"生产中…":task.status==="FAILED"?"重试生产失败阶段":production?"继续自动生产":"开始自动生产"}</Button><Link href={`/tasks/${task.id}`} className={buttonVariants({variant:"outline",size:"sm"})}>任务详情</Link>{(reviewGate || task.status==="WAITING_REVIEW")&&<Link href={`/review?task=${task.id}`} className={buttonVariants({variant:"outline",size:"sm"})}>处理当前审核</Link>}</div>
    <p className="mt-3 text-[10px] leading-5 text-muted-foreground">{localCutout?"本地原片只执行抠像与质检，人物审核后可选择已有/上传背景合成；不会启动采集、付费提示词或20张背景生成。":"先生成与合成 01，首条成片人工批准后自动完成 02–20；换背景不会重新抠像。"}</p>
    {queue.isError&&<p role="alert" className="mt-2 text-xs text-red-600">队列状态读取失败：{queue.error.message}</p>}
  </div>;
}

export function IssueTimeline({segments,duration,onSeek,onSelect}:{segments:IssueSegment[];duration:number;onSeek?:(seconds:number)=>void;onSelect?:(segment:IssueSegment)=>void}) {
  function select(segment:IssueSegment){onSeek?.(segment.start);onSelect?.(segment);}
  return <><div className="relative h-11 overflow-hidden rounded-lg border bg-muted/30" aria-label="异常区间时间轴">{Array.from({length:11},(_,index)=><span key={index} className="absolute inset-y-0 border-l" style={{left:`${index*10}%`}}/>)}{segments.map((segment,index)=><button key={`${segment.start}-${index}`} title={`${issueTime(segment.start)} – ${issueTime(segment.end)} ${segment.issue || segment.type}`} aria-label={`查看异常区间 ${issueTime(segment.start)} 至 ${issueTime(segment.end)}`} onClick={()=>select(segment)} className="absolute inset-y-1 min-w-1 rounded border border-red-500/40 bg-red-500/20" style={{left:`${duration>0?segment.start/duration*100:0}%`,width:`${duration>0?(segment.end-segment.start)/duration*100:0}%`}} />)}</div><div className="mt-2 flex justify-between font-mono text-[9px] text-muted-foreground"><span>00:00</span><span>{durationLabel(duration)}</span></div><div className="mt-4 grid gap-2 sm:grid-cols-2">{segments.map((segment,index)=><button key={index} onClick={()=>select(segment)} className="rounded-lg border px-4 py-3 text-left text-[11px]"><span className="tabular-nums text-muted-foreground">{issueTime(segment.start)} – {issueTime(segment.end)}</span><span className="ml-3 text-red-600 dark:text-red-400">{segment.issue || segment.type}</span>{segment.message&&<p className="mt-1 text-[10px] text-muted-foreground">{segment.message}</p>}</button>)}</div>{!segments.length&&<p className="mt-3 text-xs text-muted-foreground">暂无已记录的异常区间。</p>}</>;
}

function BackgroundWorkbench({task,factory,readiness}:{task:Task;factory:ReturnType<typeof useFactory>;readiness:ReturnType<typeof useProductionReadiness>}) {
  const promptReadiness=useProductionReadiness("prompt",task.id);
  const media=useQuery({queryKey:["asset-media-status","production"],queryFn:()=>request<{asset_id:string;available:boolean;preview_available:boolean;status:string}[]>("/api/assets/media-status?scope=production"),refetchInterval:5000});
  const notice=useNotice();const upload=useRef<HTMLInputElement>(null);
  const [selectedPrompt,setSelectedPrompt]=useState("");const [provider,setProvider]=useState("chatgpt_rpa");const [uploading,setUploading]=useState(false);
  const assets=factory.data?.assets.filter(asset=>asset.task_id===task.id && asset.legacy_reference?.execution_scope!=="TECHNICAL_VALIDATION") || [];
  const source=assets.filter(asset=>asset.type==="source" && asset.registry_status==="registered" && !["failed","rejected","FAILED","REJECTED"].includes(asset.status)).at(-1);
  const production=task.context.production as ProductionContext | undefined;
  const persons=assets.filter(asset=>asset.type==="person" && asset.registry_status==="registered" && !asset.legacy_reference?.superseded_by_person_asset_id);
  const person=persons.find(asset=>asset.id===production?.current_person_asset_id) || persons.at(-1);
  const backgrounds=assets.filter(asset=>asset.type==="background");
  const promptMap=new Map<number,Prompt>();
  for(const item of factory.data?.prompts || [])if(item.task_id===task.id && item.ordinal && item.ordinal>=1 && item.ordinal<=20 && item.status!=="MOCK")promptMap.set(item.ordinal,item);
  const prompts=[...promptMap.values()].sort((a,b)=>(a.ordinal || 0)-(b.ordinal || 0));
  const prompt=prompts.find(item=>item.id===selectedPrompt) || prompts[0];
  const runs=factory.data?.runs.filter(run=>run.task_id===task.id && ["background","prompt"].includes(run.stage)) || [];
  const promptRun=runs.filter(run=>run.stage==="prompt").at(-1);
  const activeRuns=factory.data?.runs.filter(run=>run.task_id===task.id && ["queued","running"].includes(run.status)) || [];
  const busy=factory.working || uploading || activeRuns.length>0;
  const unknown=factory.data?.runs.find(run=>run.task_id===task.id && run.error_code==="UNKNOWN_EXTERNAL_REQUEST");
  const taskRejected=task.status==="REJECTED";
  const first=production?.first_composition;
  const firstApproved=!!person && !!first?.composition_asset_id && first.person_asset_id===person.id && factory.data?.reviews.some(review=>review.asset_id===first.composition_asset_id && review.status==="approved");
  function fileStatus(asset:Asset) {
    if(media.isPending || media.isError)return "UNKNOWN";
    const value=media.data?.find(item=>item.asset_id===asset.id);
    if(!value)return "UNKNOWN";
    return asset.registry_status==="registered" && value.available && value.preview_available?"READY":"OUTPUT_NOT_FOUND";
  }
  function slot(item?:Prompt) {
    const history=item?backgrounds.filter(asset=>asset.legacy_reference?.prompt_id===item.id):[];
    const asset=history.at(-1);
    const result=item?production?.results?.[`background:${item.id}`]:undefined;
    const error=item?factory.data?.errors.filter(error=>error.task_id===task.id && error.stage==="background" && error.details?.prompt_id===item.id).at(-1):undefined;
    const active=item && production?.active_stage==="background" && production.active_prompt_id===item.id?activeRuns.find(run=>run.id===production.active_run_id && run.stage==="background"):undefined;
    const linkedRuns=runs.filter(run=>run.stage==="background" && (run.id===result?.run_id || run.id===error?.details?.run_id || history.some(asset=>asset.legacy_reference?.processing_run_id===run.id) || run.id===active?.id));
    const run=linkedRuns.at(-1);
    const mediaStatus=asset?fileStatus(asset):undefined;
    const ready=mediaStatus==="READY" && asset?.status==="READY";
    let status="WAITING";
    if(active)status=active.status==="queued"?"QUEUED":"RUNNING";
    else if(run?.error_code==="UNKNOWN_EXTERNAL_REQUEST")status="REVIEW";
    else if(run && run.id===result?.run_id && ["WAITING_MANUAL","WAITING_REVIEW","UNKNOWN","NOT_CONNECTED"].includes(result?.status || ""))status=result?.status==="WAITING_MANUAL"?"WAITING_MANUAL":"REVIEW";
    else if(run?.status==="failed" || run?.status==="cancelled")status="FAILED";
    else if(ready)status="READY";
    else if(asset)status=mediaStatus==="READY"?asset.status:mediaStatus || "UNKNOWN";
    else if(result?.status==="READY")status=media.isPending || media.isError?"UNKNOWN":"OUTPUT_NOT_FOUND";
    else if(item?.status && item.status!=="READY")status=["FAILED","failed"].includes(item.status)?"FAILED":"REVIEW";
    return {asset,run,linkedRuns,status,ready,result,mediaStatus};
  }
  const selected=slot(prompt);
  const selectedRuns=selected.linkedRuns;
  const backgroundBlocked=taskRejected?"原片已被拒绝，不能启动后续生产":unknown?"UNKNOWN_EXTERNAL_REQUEST：请先人工确认外部结果":!person?"等待生产线生成并核验 RGB + Alpha 母资产":!prompt || prompt.status!=="READY"?"等待生产线保存当前任务的真实提示词":(prompt.ordinal || 1)>1 && !firstApproved?"首条成片审核批准后，后台会自动生成 02–20":!readiness.data?.ready?"ChatGPT / RPA 尚未就绪；准备完成后继续自动生产，或上传真实背景":"";
  async function copyPrompt(){if(!prompt)return;try{await navigator.clipboard.writeText(`${prompt.prompt}\n\nNegative Prompt:\n${prompt.negative_prompt}`);notice("正负提示词已复制");}catch{notice("请在文本框中选择提示词复制");}}
  async function uploadBackground(file?:File){if(!file || !prompt)return;setUploading(true);try{const result=await request<{continuation?:{accepted?:boolean;message?:string}}>(`/api/background/manual?task_id=${encodeURIComponent(task.id)}&prompt_id=${encodeURIComponent(prompt.id)}`,{method:"POST",headers:{"Content-Type":file.type,"X-Filename":encodeURIComponent(file.name)},body:file});await factory.refresh();await media.refetch();notice(`背景 ${String(prompt.ordinal).padStart(2,"0")} 已核验保存；${result.continuation?.accepted?"已自动继续生产":result.continuation?.message || "等待当前生产前置条件"}`);}catch(error){notice(error instanceof Error?error.message:"背景上传失败");}finally{setUploading(false);}}
  function dependencies(check:ReturnType<typeof useProductionReadiness>,label:string) {
    return <div className="mt-5 border-t pt-4"><div className="flex items-center justify-between gap-3"><p className="text-[10px] font-medium">{label}</p><Button size="sm" variant="ghost" disabled={check.isFetching} onClick={()=>check.check()}>{check.isFetching?"检查中…":"检查连接"}</Button></div>{check.isPending?<p role="status" className="text-[10px] text-muted-foreground">正在读取真实依赖状态…</p>:check.isError?<p role="alert" className="text-[10px] text-red-600">{check.error.message}</p>:<div className="mt-3 space-y-2">{check.data.checks.filter(item=>item.required!==false).map(item=><div key={item.name} className="flex flex-wrap justify-between gap-2 text-[10px]"><span>{item.name}</span><span>{item.status}</span>{item.message&&<p className="w-full break-words leading-5 text-muted-foreground">{item.message}</p>}</div>)}</div>}</div>;
  }
  return <div className="space-y-6">
    <AutomaticProductionControl task={task} factory={factory}/>
    <div className="grid items-start gap-7 xl:grid-cols-2">
      <Panel title="纯人像视频" description={`${task.country} · ${task.keyword} · REAL`}><AssetPreview asset={person || source}/><p className="mt-4 break-all text-[10px] text-muted-foreground">{person?`复用母资产 ${person.id}`:"纯人像未产出时展示原片；不会以原片登记为 Person Asset。"}</p>{person&&<p className="mt-2 text-[10px] text-muted-foreground">当前版本审核：{factory.data?.reviews.filter(review=>review.asset_id===person.id).at(-1)?.status || "UNKNOWN"} · <Link href={`/review?task=${task.id}`} className="underline underline-offset-4">查看审核</Link></p>}</Panel>
      <div className="space-y-6">
        <Panel title="背景生成器" action={<RunLogDrawer runs={runs}/>}>
          <div className="grid gap-4 sm:grid-cols-2"><Field label="国家"><Input readOnly value={task.country}/></Field><Field label="产品"><Input readOnly value={factory.data?.products.find(product=>product.id===task.product_id)?.name || task.keyword}/></Field><Field label="场景 · 已保存提示词"><Input readOnly value={prompt?.scene_type || "尚未保存场景"}/></Field><Field label="画面比例 · 已保存提示词"><Input readOnly value={prompt?.aspect_ratio || "UNKNOWN"}/></Field></div>
          <p className="mt-4 text-[10px] leading-5 text-muted-foreground">只读显示任务与已保存 Prompt；生成沿用现有生产配置，不在此修改旧配置。</p>
          <div className="mt-5 flex flex-wrap items-center gap-3"><StatusBadge status={promptRun?.status || "WAITING"}/><p className="text-[10px] text-muted-foreground">提示词由生产线自动生成与复用，无需单独启动。</p></div>
          <p className="mt-3 text-[10px] leading-5 text-muted-foreground">可能调用已配置的付费 API；结果未确认时不会自动重复提交。</p>
          {dependencies(promptReadiness,"提示词依赖 · 不检查 RPA")}
        </Panel>
        <Panel title="提示词" description={`${prompts.length} / 20 组已保存 · 每组独立关联背景`} action={<Button size="sm" variant="ghost" disabled={!prompt} title={!prompt?"还没有实际提示词可复制":"复制当前编号的正负提示词"} onClick={copyPrompt}><Copy size={13}/>复制</Button>}>
          <select aria-label="选择背景提示词" className="mb-4 h-9 w-full rounded-lg border bg-background px-3 text-xs" value={prompt?.id || ""} onChange={event=>setSelectedPrompt(event.target.value)}>{!prompts.length&&<option value="">等待真实提示词</option>}{prompts.map(item=><option key={item.id} value={item.id}>Prompt {String(item.ordinal).padStart(2,"0")} · {item.status || "UNKNOWN"}</option>)}</select>
          <Field label="Prompt"><Textarea rows={5} readOnly value={prompt?.prompt || ""} placeholder="未生成实际提示词，不显示示例正文"/></Field><div className="mt-4"><Field label="Negative Prompt"><Textarea rows={3} readOnly value={prompt?.negative_prompt || ""}/></Field></div>{prompt?.error&&<p className="mt-3 text-xs text-red-600">{prompt.error}</p>}
          <div className="mt-5 grid grid-cols-4 gap-2 sm:grid-cols-5" aria-label="20 个背景编号状态">{Array.from({length:20},(_,index)=>{const item=promptMap.get(index+1);const current=slot(item);return <button key={index+1} aria-pressed={item?.id===prompt?.id && !!item} disabled={!item} title={!item?"该编号尚未保存真实提示词":`Prompt ${index+1}：${item.status || "UNKNOWN"}；背景：${current.status}`} onClick={()=>item&&setSelectedPrompt(item.id)} className={`min-w-0 rounded-lg border p-2 text-left text-[9px] disabled:opacity-50 ${item?.id===prompt?.id && item?"border-foreground":""}`}><span className="font-mono">{String(index+1).padStart(2,"0")}</span><span className="mt-1 block break-words text-muted-foreground">{current.status}</span></button>;})}</div>
          <p className="mt-3 text-[10px] leading-5 text-muted-foreground">编号只是生产计划；READY 必须对应已核验的背景文件。先完成并审核 01 成片，再扩展 02–20。</p>
        </Panel>
      </div>
    </div>
    <Panel title="Background Provider" description="每个 Prompt 的背景独立保存，不覆盖历史。" action={<RunLogDrawer runs={selectedRuns} label="当前背景日志"/>}>
      <div className="grid gap-3 sm:grid-cols-4">{[["chatgpt_rpa","ChatGPT / RPA"],["manual","Manual Upload"],["local_comfyui","Local ComfyUI"],["other","Other"]].map(([value,label])=><button key={value} disabled={!["chatgpt_rpa","manual"].includes(value)} title={["chatgpt_rpa","manual"].includes(value)?label:"尚未连接真实背景 Provider，不能执行"} onClick={()=>setProvider(value)} aria-pressed={provider===value} className={`rounded-lg border p-4 text-left text-xs disabled:opacity-50 ${provider===value?"border-foreground":""}`}><p>{label}</p><p className="mt-2 text-[9px] text-muted-foreground">{["chatgpt_rpa","manual"].includes(value)?"REAL ADAPTER":"NOT CONNECTED · 不可执行"}</p></button>)}</div>
      <div className="mt-5 flex flex-wrap items-center justify-between gap-4 border-t pt-5"><div><StatusBadge status={selected.status}/><p className="mt-2 text-[10px] text-muted-foreground">{prompt?`背景 ${String(prompt.ordinal).padStart(2,"0")} · ${provider==="manual"?"上传后验证图片并登记新版本":"保留请求、领取、响应与背景文件记录"}`:"先保存当前任务的真实提示词，再生成或上传背景"}</p>{selected.run&&<p className="mt-2 break-all font-mono text-[9px] text-muted-foreground">Run: {selected.run.id}</p>}{selected.result?.error_code&&<p role="alert" className="mt-2 break-words text-[10px] text-red-600">{selected.result.error_code} · {selected.result.error_message}</p>}</div>
        {provider==="manual"?<><input ref={upload} type="file" accept="image/png,image/jpeg,image/webp" className="hidden" aria-label="上传背景图片" onChange={event=>{void uploadBackground(event.target.files?.[0]);event.target.value="";}}/><Button disabled={!prompt || prompt.status!=="READY" || busy || taskRejected} title={!prompt?"等待真实提示词":busy?"当前任务执行中，请完成后上传":taskRejected?"任务已拒绝":"为选中的编号上传真实背景文件；核验后自动续跑"} onClick={()=>upload.current?.click()}><Upload size={13}/>{uploading?"上传中…":"选择背景图片"}</Button></>:selected.status==="FAILED"?<Button disabled={busy || !!backgroundBlocked} title={backgroundBlocked || "仅重试选中失败项，保存新 Run，完成后自动续跑"} onClick={()=>factory.act("production.retry",task.id,{task_ids:[task.id],stage:"background",prompt_id:prompt?.id})}>重试失败背景 {String(prompt?.ordinal || 1).padStart(2,"0")}</Button>:<p className="text-[10px] text-muted-foreground">由后台 Job 自动消费提示词，无需逐张点击生成。</p>}
      </div>
      {provider==="chatgpt_rpa"&&<><p className="mt-3 text-[10px] leading-5 text-muted-foreground">{backgroundBlocked || "后台先完成 01；首次审核批准后串行执行 02–20。可能消耗图片额度；UNKNOWN 请求不重交。"}</p>{dependencies(readiness,"背景依赖 · ChatGPT / RPA")}</>}
      {selected.asset&&<div className="mt-5 border-t pt-5">{selected.mediaStatus==="READY"?<AssetPreview asset={selected.asset} label={`背景 ${prompt?.ordinal || ""}`}/>:<p role="status" className="text-xs text-muted-foreground">{selected.mediaStatus==="UNKNOWN"?"媒体状态 UNKNOWN：尚未完成真实文件核验":"OUTPUT_NOT_FOUND：媒体不存在或与登记记录不一致"}</p>}<Link href={`/assets/${selected.asset.id}`} className="mt-3 block break-all text-[10px] underline underline-offset-4">查看当前背景资产 {selected.asset.id}</Link></div>}
    </Panel>
    <Panel title="背景历史" description={`${backgrounds.length} 张独立背景资产`} action={<Button size="sm" variant="ghost" disabled={factory.isFetching || media.isFetching} onClick={()=>{void factory.refresh();void media.refetch();}}>刷新资产</Button>}><div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">{backgrounds.map(asset=><Link key={asset.id} href={`/assets/${asset.id}`} className="rounded-lg border p-3"><p className="text-xs font-medium">{asset.name}</p><p className="mt-2 text-[10px] text-muted-foreground">{asset.width} × {asset.height} · v{asset.version || asset.legacy_reference?.version as number || 1}</p><p className="mt-2 break-all text-[9px] text-muted-foreground">{asset.id}</p><div className="mt-3"><StatusBadge status={fileStatus(asset)==="READY"?asset.status:fileStatus(asset)}/></div></Link>)}</div>{!backgrounds.length&&<p className="text-xs text-muted-foreground">尚无背景图片。真实生成完成或手动上传并核验后显示；不会创建假背景资产。</p>}{media.isError&&<p role="alert" className="mt-3 text-xs text-red-600">媒体核验失败：{media.error.message}；不使用数据库记录替代文件证据。</p>}</Panel>
  </div>;
}

function CompositionWorkbench({task,factory}:{task:Task;factory:ReturnType<typeof useFactory>}) {
  const [selectedOrdinal,setSelectedOrdinal]=useState(1);
  const production=task.context.production as ProductionContext | undefined;
  const assets=factory.data?.assets.filter(asset=>asset.task_id===task.id && asset.registry_status==="registered" && asset.legacy_reference?.execution_scope!=="TECHNICAL_VALIDATION") || [];
  const person=assets.find(asset=>asset.id===production?.current_person_asset_id && asset.type==="person");
  const source=assets.filter(asset=>asset.type==="source").at(-1);
  const promptMap=new Map<number,Prompt>();
  for(const prompt of factory.data?.prompts || [])if(prompt.task_id===task.id && prompt.ordinal && prompt.status!=="MOCK")promptMap.set(prompt.ordinal,prompt);
  const runs=factory.data?.runs.filter(run=>run.task_id===task.id && ["compose","quality"].includes(run.stage)) || [];
  const media=useQuery({queryKey:["asset-media-status","production"],queryFn:()=>request<{asset_id:string;available:boolean;preview_available:boolean;status:string}[]>("/api/assets/media-status?scope=production"),refetchInterval:5000});
  const snapshots=useQuery({queryKey:["workflow-snapshots",task.id],queryFn:()=>request<{id:string;processing_run_id:string;sha256:string}[]>(`/api/tasks/${encodeURIComponent(task.id)}/workflows`),refetchInterval:5000});
  function slot(ordinal:number) {
    const prompt=promptMap.get(ordinal);
    const backgrounds=assets.filter(asset=>asset.type==="background" && prompt && asset.legacy_reference?.prompt_id===prompt.id);
    const background=backgrounds.at(-1);
    const history=assets.filter(asset=>asset.type==="compose" && prompt && asset.legacy_reference?.prompt_id===prompt.id);
    const composition=history.filter(asset=>asset.legacy_reference?.person_asset_id===person?.id && asset.legacy_reference?.background_asset_id===background?.id).at(-1);
    const result=prompt?production?.results?.[`compose:${prompt.id}`]:undefined;
    const error=prompt?factory.data?.errors.filter(item=>item.task_id===task.id && item.stage==="compose" && item.details?.prompt_id===prompt.id).at(-1):undefined;
    const active=prompt && production?.active_stage==="compose" && production.active_prompt_id===prompt.id?runs.find(run=>run.id===production.active_run_id && run.stage==="compose" && ["queued","running"].includes(run.status)):undefined;
    const linkedRuns=runs.filter(run=>run.stage==="compose" && (run.id===composition?.legacy_reference?.processing_run_id || run.id===result?.run_id || run.id===error?.details?.run_id || run.id===active?.id));
    const run=linkedRuns.at(-1);
    const file=composition?media.data?.find(item=>item.asset_id===composition.id):undefined;
    const quality=factory.data?.quality_assessments?.filter(item=>item.asset_id===composition?.id).at(-1);
    const review=factory.data?.reviews.filter(item=>item.asset_id===composition?.id).at(-1);
    let status="WAITING";
    if(active)status=active.status==="queued"?"QUEUED":"RUNNING";
    else if(composition && (media.isPending || media.isError))status="UNKNOWN";
    else if(composition && (!file?.available || !file.preview_available))status="OUTPUT_NOT_FOUND";
    else if(composition && quality?.status==="FAIL")status="QUALITY_FAIL";
    else if(composition && review?.status==="rejected")status="REJECTED";
    else if(composition && review?.status!=="approved")status="WAITING_REVIEW";
    else if(composition)status="READY";
    else if(run?.error_code==="UNKNOWN_EXTERNAL_REQUEST")status="REVIEW";
    else if(run?.status==="failed" || run?.status==="cancelled")status="FAILED";
    else if(history.length)status="STALE_INPUT_VERSION";
    else if(result && result.status!=="READY")status=result.status || "UNKNOWN";
    return {prompt,person,background,composition,linkedRuns,run,quality,review,status,file};
  }
  const selected=slot(selectedOrdinal);
  const composition=selected.composition;
  const completed=Array.from({length:20},(_,index)=>slot(index+1)).filter(item=>!!item.composition && item.file?.available && item.file.preview_available);
  const selectedSnapshot=snapshots.data?.find(snapshot=>snapshot.processing_run_id===selected.run?.id);
  const busy=factory.working || factory.data?.runs.some(run=>run.task_id===task.id && ["queued","running"].includes(run.status));
  const firstApproved=production?.first_composition?.human_review==="APPROVED" || factory.data?.reviews.some(review=>review.asset_id===production?.first_composition?.composition_asset_id && review.status==="approved");
  return <div className="space-y-6">
    <AutomaticProductionControl task={task} factory={factory}/>
    <div className="grid gap-7 xl:grid-cols-[minmax(0,1fr)_320px]">
      <div className="space-y-6"><Panel title="合成输入" description="只读复用已登记 RGB + Alpha；合成不会运行 BiRefNet / SAM" action={<RunLogDrawer runs={selected.linkedRuns}/>}>
        <div className="grid gap-5 sm:grid-cols-2"><div><p className="mb-3 text-xs">Person Video</p><AssetPreview asset={person}/><p className="mt-3 break-all font-mono text-[9px] text-muted-foreground">{person?.id || "等待当前母资产"}</p></div><div><p className="mb-3 text-xs">Background {String(selectedOrdinal).padStart(2,"0")}</p><AssetPreview asset={selected.background}/><p className="mt-3 break-all font-mono text-[9px] text-muted-foreground">{selected.background?.id || "等待真实背景"}</p></div></div>
        <p className="mt-5 flex items-center justify-center gap-2 border-t pt-4 text-xs text-muted-foreground"><ArrowDown size={14}/>复用同一母资产并保留原音轨</p>
      </Panel><Panel title={`合成成片 ${String(selectedOrdinal).padStart(2,"0")}`} action={<StatusBadge status={selected.status}/>}>
        {selected.file?.available && selected.file.preview_available?<AssetPreview asset={composition}/>:<p role="status" className="rounded-lg border p-6 text-xs leading-6 text-muted-foreground">{composition?selected.status:"本编号尚无当前 RGB / Alpha 与背景对应的真实成片；不展示历史版本充当本次结果。"}</p>}
        {composition&&<Link href={`/assets/${composition.id}`} className="mt-4 block break-all text-xs underline underline-offset-4">查看成片、Run、ffprobe、Hash 与资产溯源 · {composition.id}</Link>}
        <div className="mt-5 space-y-2 border-t pt-4 text-[10px] text-muted-foreground"><p>Quality：{selected.quality?.status || "UNKNOWN"} · 人工审核：{selected.review?.status || "UNKNOWN"}</p><p className="break-all">Run：{selected.run?.id || "尚未执行"}</p><p className="break-all">Workflow SHA-256：{selectedSnapshot?.sha256 || "UNKNOWN"}</p><p className="break-all">Composition SHA-256：{composition?.sha256 || "UNKNOWN"}</p></div>
        {selected.run?.error_code&&<p role="alert" className="mt-4 text-xs leading-6 text-red-600">{selected.run.error_code} · {selected.run.error_message}</p>}
        {selected.status==="FAILED"&&<Button className="mt-4" variant="outline" disabled={!!busy || !selected.prompt || !person || !selected.background || selectedOrdinal>1&&!firstApproved} title={selectedOrdinal>1&&!firstApproved?"首条成片尚未批准；不能扩展02–20":"只重试本编号合成，保存新版本，复用当前母资产与背景"} onClick={()=>factory.act("production.retry",task.id,{task_ids:[task.id],stage:"compose",person_asset_id:person?.id,prompt_id:selected.prompt?.id})}>重试失败成片 {String(selectedOrdinal).padStart(2,"0")}</Button>}
      </Panel></div>
      <aside className="space-y-6"><Panel title="调色与曝光 · 生产工作流"><p className="text-xs leading-6 text-muted-foreground">沿用 Factory 当前 API 工作流的实际调色节点。本页不提供尚未连接处理器的参数草稿。</p><p className="mt-4 text-[10px] leading-5 text-muted-foreground">运行参数与节点连接保存于每次工作流快照，可从当前成片详情查看，不覆盖旧工作流。</p></Panel><Panel title="输出规格"><p className="text-2xl font-medium">{composition?`${composition.width} × ${composition.height}`:"等待实际成片"}</p><dl className="mt-5 space-y-3 text-xs">{[["原始 FPS",source?.fps || "UNKNOWN"],["输出 FPS",composition?.fps || "UNKNOWN"],["实际编码",composition?.codec || "UNKNOWN"],["实际时长",composition?`${composition.duration}s`:"UNKNOWN"],["实际帧数",composition?.probe_metadata?.frame_count ?? "UNKNOWN"]].map(([label,value])=><div key={String(label)} className="flex justify-between gap-3"><dt>{String(label)}</dt><dd>{String(value)}</dd></div>)}</dl><p className="mt-4 text-[10px] leading-5 text-muted-foreground">尺寸与帧率来自真实 ffprobe，未生成前不声明固定输出结果。</p></Panel></aside>
    </div>
    <Panel title="20 条合成队列" description={`${completed.length} / 20 条已存在真实文件；首条批准后自动推进后续编号`}>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-5" aria-label="20 条成片编号状态">{Array.from({length:20},(_,index)=>{const item=slot(index+1);return <button key={index+1} aria-pressed={selectedOrdinal===index+1} onClick={()=>setSelectedOrdinal(index+1)} className={`rounded-lg border p-3 text-left text-xs ${selectedOrdinal===index+1?"border-foreground":""}`}><span className="font-mono">{String(index+1).padStart(2,"0")}</span><span className="mt-2 block break-words text-[9px] text-muted-foreground">{item.status}</span></button>;})}</div>
      <p className="mt-4 break-all text-[10px] leading-5 text-muted-foreground">当前 PersonAsset：{person?.id || "未产出"}。{completed.length} 条当前成片均明确关联该母资产；更改 Alpha 后旧成片保留，但不会算作当前版本完成。</p>
      {media.isError&&<p role="alert" className="mt-3 text-xs text-red-600">媒体核验失败：{media.error.message}</p>}
    </Panel>
  </div>;
}

export function RealWorkbench({kind,task,factory}:{kind:"cutout"|"background"|"compose";task:Task;factory:ReturnType<typeof useFactory>}) {
  const readiness=useProductionReadiness(kind,task.id);
  const [selectedPerson,setSelectedPerson]=useState("");
  const [cutoutChannel,setCutoutChannel]=useState("source");const video=useRef<HTMLVideoElement>(null);const [playbackTime,setPlaybackTime]=useState(0);
  const assets=factory.data?.assets.filter(asset=>asset.task_id===task.id) || [];
  const source=assets.find(asset=>asset.type==="source");const persons=assets.filter(asset=>asset.type==="person");const currentPersonId=(task.context.production as Record<string,unknown> | undefined)?.current_person_asset_id;const person=persons.find(asset=>asset.id===(selectedPerson || currentPersonId)) || persons.at(-1);
  const composition=assets.filter(asset=>["compose","final"].includes(asset.type) && asset.legacy_reference?.person_asset_id===person?.id && !asset.legacy_reference?.superseded_by_person_asset_id).at(-1);
  const runs=factory.data?.runs.filter(run=>run.task_id===task.id && (run.stage===kind || (kind==="cutout" && run.stage==="quality") || (kind==="background" && run.stage==="prompt"))) || [];
  const quality=factory.data?.quality_assessments?.filter(item=>item.asset_id===(person?.id || composition?.id)).at(-1);
  const busy=factory.working || factory.data?.runs.some(run=>run.task_id===task.id && ["queued","running"].includes(run.status));
  const boundary=readiness.data?.checks.find(check=>check.error_code==="CUTOUT_OUTPUT_BOUNDARY");
  const operationReady=readiness.data?.ready;
  const localSource=task.context.local_cutout as {source_asset_id?:string;full_decode_verified?:boolean}|undefined;
  const sourceApproved=task.collector_result.decision==="accepted" || !!(localSource?.full_decode_verified && localSource.source_asset_id===source?.id && source?.source_system==="FactoryLocalUpload") || factory.data?.reviews.some(review=>review.task_id===task.id && review.asset_id===source?.id && review.status==="approved");
  const blocked=boundary?`CUTOUT_OUTPUT_BOUNDARY：${boundary.message}`:!source?"等待核验后的原始视频资产":kind==="cutout" && !sourceApproved?"先在审核中心完成原片准入审核":!operationReady?"当前操作依赖尚未就绪，请查看生产就绪状态":"";
  const rerun=(target:string)=>factory.act("production.retry",task.id,{stage:target});
  if(kind==="cutout") {
    const preview=cutoutChannel==="source"?source:cutoutChannel==="composite"?composition:person;
    const cutoutRun=runs.filter(item=>item.stage==="cutout").at(-1);
    let origin=person;
    const visited=new Set<string>();
    while(origin?.legacy_reference?.parent_person_asset_id && !visited.has(origin.id)) {
      visited.add(origin.id);
      origin=persons.find(asset=>asset.id===origin?.legacy_reference?.parent_person_asset_id);
    }
    const originRun=runs.find(run=>run.stage==="cutout" && run.id===origin?.legacy_reference?.processing_run_id);
    const displayedRun=cutoutRun && ["running","queued"].includes(cutoutRun.status)?cutoutRun:originRun || cutoutRun;
    const cutoutStatus=displayedRun?.status || (operationReady?"pending":"not_connected");
    const cutoutProgress=cutoutStatus==="success"?100:displayedRun?.progress || 0;
    return <div className="space-y-6"><AutomaticProductionControl task={task} factory={factory}/><div className="grid items-start gap-7 lg:grid-cols-[minmax(0,1fr)_340px]">
      <Panel title="视频检查" description={`${task.country} · ${task.keyword} · REAL`} action={<RunLogDrawer runs={runs}/>}>
        <div className="mb-5 flex flex-wrap items-center justify-between gap-3"><div role="tablist" aria-label="抠像预览通道" className="flex gap-1">{[["source","原视频"],["alpha","Alpha"],["black","Person"],["composite","合成预览"]].map(([value,label])=><button key={value} role="tab" aria-selected={cutoutChannel===value} disabled={value==="source"?!source:value==="composite"?!composition:!person} title={value==="composite"&&!composition?"尚未生成真实合成资产":value!=="source"&&!person?"等待独立 RGB + Alpha 母资产":label} onClick={()=>setCutoutChannel(value)} className={`rounded-lg px-3 py-2 text-xs disabled:opacity-40 ${cutoutChannel===value?"bg-muted font-medium":"text-muted-foreground"}`}>{label}</button>)}</div>
          <select aria-label="选择纯人像版本" className="rounded-lg border bg-background p-2 text-xs" value={person?.id || ""} onChange={event=>setSelectedPerson(event.target.value)}>{!persons.length&&<option value="">尚无 Person 版本</option>}{persons.map((asset,index)=><option key={asset.id} value={asset.id}>Person v{asset.version || asset.legacy_reference?.version as number || index+1} · {asset.status}</option>)}</select>
        </div>
        <AssetPreview asset={preview} label={`抠像${cutoutChannel}预览`} channel={cutoutChannel==="alpha"?"alpha":"black"} videoRef={video} startAt={playbackTime} onTimeUpdate={setPlaybackTime}/>
        <p className="mt-4 text-[10px] text-muted-foreground">{source?`原始 ${source.width} × ${source.height} · ${source.fps} FPS · ${source.codec}`:"原始规格 UNKNOWN"}</p>
        {person&&<p className="mt-2 break-all text-[10px] text-muted-foreground">母资产 {person.id} · 独立 RGB + Alpha；黑底视频仅用于预览。</p>}
      </Panel>
      <aside className="space-y-6"><Panel title="抠像状态" description="REAL · 当前生产模型"><div className="space-y-5">{["BiRefNet","VideoAutoMatteRepair"].map(name=><div key={name} className="flex items-center justify-between"><span className="text-xs">{name}</span><StatusBadge status={cutoutStatus}/></div>)}</div><div className="mt-5 border-t pt-5"><p className="mb-3 text-[10px] text-muted-foreground">{cutoutProgress.toFixed(1)}%</p><ProgressBar value={cutoutProgress}/></div>
        {person&&<p className="mt-3 text-[10px] leading-5 text-muted-foreground">显示当前母资产来源的真实抠像执行；修复版本继续复用该 RGB，质量结论见下方检测。</p>}
        {cutoutRun && displayedRun?.id!==cutoutRun.id&&<p className="mt-2 text-[10px] leading-5 text-muted-foreground">最近重新抠像：{cutoutRun.error_code==="PROCESS_CANCELLED"?"已取消":cutoutRun.status}，历史 Run 保留，未替换当前母资产。</p>}
        {person&&<Button className="mt-5 w-full" variant="outline" disabled={busy || !!blocked} title={blocked || "明确重新抠像整条原片并保存新版本；局部缺损请前往审核修复"} onClick={()=>rerun("cutout")}>重新抠像整条原片 · 新版本</Button>}{blocked&&<p className="mt-3 break-words text-[10px] leading-5 text-muted-foreground">{blocked}</p>}
        <p className="mt-3 text-[10px] leading-5 text-muted-foreground">一次抠像生成可复用 RGB + Alpha；新处理保存新版本，原片与历史资产保留。</p>
      </Panel><Panel title="质量检查" description="OpenCV / NumPy 几何与时序检测；不确认人体或产品语义完整性" action={<Button size="sm" variant="ghost" disabled={busy || !person || person.id!==currentPersonId} title="只检测当前生产 Alpha；不重新抠像或生成背景" onClick={()=>factory.act("production.retry",task.id,{stage:"quality",person_asset_id:person?.id})}>检测当前 Alpha</Button>}><StatusBadge status={quality?.status || "UNKNOWN"}/><div className="mt-4 space-y-4">{[["Quality Score",quality?.quality_score],["Mask Area",quality?.metrics.mask_area_mean],["Temporal Stability",quality?.metrics.temporal_stability],["Person Presence",quality?.metrics.person_presence_ratio],["Body Integrity（几何）",quality?.metrics.body_integrity_proxy_min],["Edge Stability",quality?.metrics.edge_stability],["Hole Ratio",quality?.metrics.hole_ratio_max],["Flicker",(quality?.metrics.issue_frame_ratios as Record<string,unknown> | undefined)?.MASK_FLICKER]].map(([label,value])=><div key={String(label)} className="flex justify-between gap-3 text-xs"><span className="text-muted-foreground">{String(label)}</span><span>{display(value)}</span></div>)}</div></Panel></aside>
    </div><Panel title="异常时间轴" description="只显示检测器记录的区间；尚未检测时不生成示例异常。"><IssueTimeline segments={quality?.issue_segments || []} duration={person?.duration || source?.duration || 0} onSeek={seconds=>{setPlaybackTime(seconds);if(video.current)video.current.currentTime=seconds;}}/>
      <div className="mt-5 flex flex-wrap gap-2">{person?<Link href={`/review?task=${task.id}`} className={buttonVariants({variant:"outline"})}>重新处理异常区间</Link>:<Button variant="outline" disabled title="先生成独立 RGB + Alpha 母资产">重新处理异常区间</Button>}{person?<Link href={`/review?task=${task.id}`} className={buttonVariants({variant:"outline"})}>重新生成 Mask</Link>:<Button variant="outline" disabled title="先生成独立 RGB + Alpha 母资产">重新生成 Mask</Button>}<Link href={`/review?task=${task.id}`} className="self-center text-xs underline underline-offset-4">前往人工审核</Link></div><p className="mt-3 text-[10px] text-muted-foreground">在现有审核入口选择对象与区间，只替换局部 Alpha 并保存新版本。</p>
    </Panel></div>;
  }
  if(kind==="background")return <BackgroundWorkbench task={task} factory={factory} readiness={readiness}/>;
  return <CompositionWorkbench task={task} factory={factory}/>;
}
