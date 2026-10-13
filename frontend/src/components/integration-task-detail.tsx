"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download, Pause, Play, RefreshCw, SkipForward, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState, PageHeading, Panel, ProgressBar, StatusBadge } from "@/components/factory-ui";
import { CollectorResult, display } from "@/components/integration-collector";
import { RunLogDrawer, WorkflowHistory } from "@/components/production-ui";
import { downloadJson, request, useFactory, useProductionQueue } from "@/lib/api";
import { useNotice } from "@/components/providers";
import { integrationStageNames, stageLabels, timeLabel, durationLabel, type Asset, type FactoryState, type StageName, type Task } from "@/lib/types";

const routes:Record<string,string>={collector:"collector",analysis:"collector",download:"assets",prompt:"background",cutout:"cutout",person:"cutout",background:"background",compose:"compose",quality:"review",review:"review",final:"assets",publish:"publish"};
const assetLabels:Record<string,string>={source:"原始视频",source_candidate:"候选链接",person:"纯人像",background:"背景",compose:"合成",final:"最终视频",exception:"异常资产"};
type TaskRecord=Pick<FactoryState,"stages"|"assets"|"prompts"|"runs"|"errors"|"audits"|"reviews"|"quality_assessments"> & {task:Task};

export function IntegrationTaskDetail({task:initialTask,factory}:{task:Task;factory:ReturnType<typeof useFactory>}) {
  const detail=useQuery({queryKey:["task-detail",initialTask.id],queryFn:()=>request<TaskRecord>(`/api/tasks/${initialTask.id}`),refetchInterval:1500});
  const queue=useProductionQueue();const notify=useNotice();
  const task=detail.data?.task || initialTask;
  const runs=detail.data?.runs || [];const assets=detail.data?.assets || [];
  const errors=detail.data?.errors || [];const audits=detail.data?.audits || [];
  const reviews=detail.data?.reviews || [];const quality=detail.data?.quality_assessments || [];
  const busy=factory.working || runs.some(run=>["queued","running"].includes(run.status));
  const production=(task.context.production || {}) as Record<string,unknown>;
  const currentPerson=assets.find(asset=>asset.type==="person" && asset.id===production.current_person_asset_id);
  const personFor=(asset:Asset)=>asset.type==="person"?asset:assets.find(person=>person.type==="person" && person.id===(asset.legacy_reference?.person_asset_id || assets.find(composition=>composition.id===asset.legacy_reference?.composition_asset_id)?.legacy_reference?.person_asset_id));
  const historical=(asset:Asset)=>!!asset.legacy_reference?.superseded_by_person_asset_id || asset.legacy_reference?.execution_scope==="TECHNICAL_VALIDATION" || ["person","compose","final"].includes(asset.type) && !!production.current_person_asset_id && personFor(asset)?.id!==production.current_person_asset_id;
  const usable=(asset:Asset)=>asset.registry_status==="registered" && !["rejected","failed"].includes(asset.status) && !historical(asset);
  let origin=currentPerson;
  const visited=new Set<string>();
  while(origin?.legacy_reference?.parent_person_asset_id) {
    if(visited.has(origin.id)){origin=undefined;break;}
    visited.add(origin.id);
    origin=assets.find(asset=>asset.type==="person" && asset.id===origin?.legacy_reference?.parent_person_asset_id);
  }
  const originRun=runs.find(run=>run.stage==="cutout" && run.id===origin?.legacy_reference?.processing_run_id);
  function stageView(name:StageName) {
    const stage=detail.data?.stages.find(item=>item.name===name);
    const stageAssets=assets.filter(asset=>asset.type===name || name==="download" && asset.type==="source");
    const currentAssets=stageAssets.filter(asset=>!historical(asset));
    const active=[...runs].reverse().find(run=>run.stage===name && run.provider!=="mock" && ["queued","running"].includes(run.status));
    let status:string=stage?.status || "not_connected";
    let error=stage?.error || "";
    let duration=stage?.start_time?stage.duration:undefined;
    if(name==="cutout" && currentPerson) {
      const selected=active || originRun;
      status=selected?.status || "UNKNOWN";error=selected?.error_message || selected?.error_code || "";
      duration=selected?.duration_ms==null?undefined:selected.duration_ms/1000;
    } else if(name==="person" && currentPerson) {
      const selected=runs.find(run=>run.id===currentPerson.legacy_reference?.processing_run_id);
      status=usable(currentPerson)?"success":currentPerson.status;error=usable(currentPerson)?"":selected?.error_message || "";
      duration=selected?.duration_ms==null?undefined:selected.duration_ms/1000;
    } else if(["compose","final"].includes(name) && production.current_person_asset_id) {
      const current=currentAssets.filter(usable).at(-1);
      const failed=name===task.current_stage && task.status==="FAILED"?[...runs].reverse().find(run=>run.stage===name && run.status==="failed"):undefined;
      const selected=active || failed || runs.find(run=>run.id===current?.legacy_reference?.processing_run_id);
      status=active?.status || (failed?"failed":current?"success":"pending");
      error=active || current?"":failed?.error_message || failed?.error_code || "";
      duration=selected?.duration_ms==null?undefined:selected.duration_ms/1000;
    }
    return {stage,status,error,duration,stageAssets,currentAssets};
  }
  const waiting=(production.waiting || {}) as Record<string,unknown>;
  const context=(task.context.task_context || {}) as Record<string,unknown>;
  const queued=queue.data?.items?.find(item=>item.task_id===task.id);
  const activeBatch=!!queued && ["QUEUED","RUNNING","PAUSED"].includes(queue.data?.status || "");
  const canResume=!!queued && (queue.data?.status==="PAUSED" || ["COMPLETED_WITH_ERRORS","COMPLETED"].includes(queue.data?.status || "") && task.status==="COLLECTED");
  const hasSavedSteps=!!task.collector_result?.video_id || !!production.current_person_asset_id;
  const canContinue=["CREATED","COLLECTED","CANCELLED","WAITING_REVIEW","WAITING_MANUAL","PAUSED"].includes(task.status);
  const latestRun=[...runs].reverse().find(run=>run.provider!=="mock");
  const checkpointRetry=task.status==="FAILED" && latestRun?.stage==="cutout"
    && latestRun.provider==="LegacyComfyUIAdapter" && latestRun.error_code==="STAGE_RESULT_INVALID"
    && latestRun.error_message==="TimeoutError" && !production.current_person_asset_id;
  const start=()=>hasSavedSteps || waiting.reason || task.status==="CANCELLED"?factory.act("production.continue",task.id):factory.act("production.start",undefined,{task_ids:[task.id]});
  async function exportMetadata(){try{downloadJson(`${task.id}-metadata.json`,await request(`/api/tasks/${task.id}/metadata`));}catch(error){notify(error instanceof Error?error.message:"无法读取元数据");}}
  const control=(action:string)=>factory.act(`production.${action}`,task.id,{batch_id:queue.data?.batch_id});

  return <>
    <Link href="/dashboard" className="mb-6 inline-flex items-center gap-2 text-xs text-muted-foreground"><ArrowLeft size={13}/>返回生产总览</Link>
    <PageHeading eyebrow={`${task.id} · REAL`} title={task.keyword} description={`${task.country} · ${factory.data?.products.find(product=>product.id===task.product_id)?.name || "产品待关联"}`}>
      <RunLogDrawer runs={runs}/><Button disabled={busy || !canContinue || task.status==="QUEUED"} title="后端只检查下一阶段的真实依赖；继续已保存步骤，不重复抠像或未知付费请求" onClick={start}><Play size={13}/>{hasSavedSteps || waiting.reason?"继续生产":"开始生产"}</Button>
    </PageHeading>
    {detail.isError&&<p role="alert" className="mb-5 text-xs text-red-600">任务 API 读取失败：{detail.error.message}<Button size="sm" variant="ghost" onClick={()=>detail.refetch()}>重新读取</Button></p>}
    <div className="mb-7 flex flex-wrap items-center justify-between gap-5 rounded-xl border p-6"><div><StatusBadge status={task.status}/><p className="mt-3 text-[10px] text-muted-foreground">当前阶段 {stageLabels[task.current_stage]} · Context {display(context.context_id)}</p></div><div className="w-full sm:w-52"><p className="mb-3 text-[10px] text-muted-foreground">生产进度 {Math.round(task.progress*10)/10}%</p><ProgressBar value={task.progress}/></div></div>
    {!!waiting.reason&&<div className="mb-7 rounded-xl border p-5"><div className="flex flex-wrap items-center justify-between gap-4"><div><StatusBadge status="WAITING_MANUAL"/><p className="mt-3 text-xs leading-6">{display(waiting.message)}</p><p className="mt-1 text-[10px] text-muted-foreground">{display(waiting.reason)} · 下一步：{stageLabels[String(waiting.next_step) as keyof typeof stageLabels] || display(waiting.next_step)}</p></div><Button size="sm" variant="outline" disabled={busy} onClick={()=>factory.act("production.continue",task.id)}>前置条件已处理，继续生产</Button></div><p className="mt-3 text-[11px] leading-6 text-muted-foreground">{display(waiting.resume)}。后台会重新核实条件；未满足时仍停在当前节点，不伪造通过。</p><p className="mt-2 text-[10px] text-muted-foreground">已保存：{assets.filter(asset=>asset.registry_status==="registered").length} 个资产 · {detail.data?.prompts.length || 0} 组提示词 · {runs.filter(run=>run.status==="success").length} 次完成执行</p></div>}
    <Panel title="生产流水线" description="各阶段使用实际状态；未连接的能力保持 NOT CONNECTED。">
      <ol aria-label="真实任务阶段" className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-6">{integrationStageNames.map((name,index)=>{const view=stageView(name);return <li key={name}><Link href={`/${routes[name]}?task=${task.id}`} className="block rounded-lg border px-3 py-4 hover:bg-muted/30"><span className="text-[10px] text-muted-foreground">{String(index+1).padStart(2,"0")}</span><p className="my-3 text-xs font-medium">{stageLabels[name]}</p><StatusBadge status={name==="publish"?"MOCK":view.status}/></Link></li>;})}</ol>
      <div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="border-b text-[10px] text-muted-foreground"><tr>{["阶段","状态","耗时","Retry","Run","Asset","异常"].map(label=><th key={label} className="px-3 py-3 font-normal">{label}</th>)}</tr></thead><tbody>{integrationStageNames.map(name=>{const view=stageView(name);const stageRuns=runs.filter(run=>run.stage===name);return <tr key={name} className="border-b last:border-0"><td className="px-3 py-4">{stageLabels[name]}</td><td className="px-3"><StatusBadge status={name==="publish"?"MOCK":view.status}/></td><td className="px-3">{view.duration==null?"—":durationLabel(view.duration)}</td><td className="px-3">{view.stage?.retry_count || 0}</td><td className="px-3"><RunLogDrawer runs={stageRuns} label={`${stageRuns.length} 次`}/></td><td className="px-3">{view.currentAssets.length}{view.stageAssets.length>view.currentAssets.length&&<span className="ml-2 text-[10px] text-muted-foreground">· {view.stageAssets.length-view.currentAssets.length} 历史</span>}</td><td className="max-w-64 whitespace-normal px-3 text-[10px] text-red-600">{view.error || "—"}</td></tr>;})}</tbody></table></div>
      {currentPerson&&<p className="mt-3 break-words text-[10px] leading-5 text-muted-foreground">阶段状态按当前 Person v{display(currentPerson.version || currentPerson.legacy_reference?.version)} 显示；抠像沿用该母资产来源的执行，最终资产必须对应当前版本。历史重试、取消和审核记录全部保留。</p>}
      <div className="mt-5 flex flex-wrap gap-2 border-t pt-5">
        <Button size="sm" variant="outline" disabled={busy || !["FAILED","WAITING_REVIEW"].includes(task.status)} title={checkpointRetry?"核实同版本检查点，复用已完成分块；原队列暂停时保持暂停，不重新抠像已完成部分":"未知付费请求不会自动重交；局部重处理从相应工作台操作"} onClick={()=>factory.act(checkpointRetry?"production.resume_cutout":"production.retry",task.id)}><RefreshCw size={13}/>从失败阶段重试</Button>
        <Button size="sm" variant="outline" disabled={!activeBatch || queue.data?.status!=="RUNNING"} onClick={()=>control("pause")}><Pause size={13}/>暂停队列</Button>
        <Button size="sm" variant="outline" disabled={!canResume || factory.working} onClick={()=>control("resume")}><Play size={13}/>恢复队列</Button>
        <Button size="sm" variant="outline" disabled={!activeBatch || queued?.status!=="QUEUED"} onClick={()=>control("skip")}><SkipForward size={13}/>跳过任务</Button>
        <Button size="sm" variant="ghost" disabled={!activeBatch || factory.working} onClick={()=>control("cancel")}><Square size={13}/>取消队列</Button>
      </div><p className="mt-3 text-[10px] leading-5 text-muted-foreground">暂停在当前阶段结束后生效；取消影响此任务所属批次，保留全部历史、资产和外部队列记录。</p>
    </Panel>
    <div className="mt-7 grid gap-7 xl:grid-cols-[minmax(0,1fr)_320px]"><div className="min-w-0 space-y-7">
      <CollectorResult task={task}/>
      <Panel title="关联资产" description="版本独立保存；已下载文件与候选链接明确区分。">{assets.length?<div className="grid gap-3 sm:grid-cols-2">{assets.map(asset=>{const person=personFor(asset);return <Link key={asset.id} href={`/assets/${asset.id}`} className="min-w-0 rounded-lg border p-4 hover:bg-muted/30"><div className="mb-3 flex flex-wrap justify-between gap-2"><span className="text-xs font-medium">{assetLabels[asset.type] || asset.type}</span><StatusBadge status={asset.status}/></div><p className="break-words text-[11px]">{asset.name}</p><p className="mt-2 text-[10px] text-muted-foreground">{asset.width && asset.height?`${asset.width} × ${asset.height} · ${asset.fps} FPS`:"媒体规格 UNKNOWN"}</p><p className="mt-2 break-words text-[10px] text-muted-foreground">{asset.legacy_reference?.execution_scope==="TECHNICAL_VALIDATION"?"技术验证 · 非生产":historical(asset)?"历史版本":asset.type==="person"?"当前母资产":"当前任务资产"} · 资产 v{display(asset.version || asset.legacy_reference?.version)}{person?` · Person v${display(person.version || person.legacy_reference?.version)} · ${person.id}`:""}</p></Link>;})}</div>:<EmptyState title="暂无媒体资产" description="实际文件核验通过后会登记在这里。"/>}</Panel>
      <Panel title="质量与审核" description="只显示此任务真实资产的检测和人工记录。">{!quality.length&&!reviews.length?<p className="text-xs text-muted-foreground">尚无质量检测；未伪造 PASS。</p>:<div className="space-y-3">{quality.map(item=><div key={item.id} className="rounded-lg border p-3"><StatusBadge status={item.status}/><span className="ml-3 text-xs">{item.quality_score ?? "UNKNOWN"}</span><p className="mt-2 break-all text-[10px] text-muted-foreground">{item.asset_id} · Run {item.processing_run_id} · {item.issue_segments.length} 个异常区间</p></div>)}{reviews.map(item=><Link key={item.id} href={`/review?task=${task.id}`} className="block rounded-lg border p-3"><StatusBadge status={item.status}/><p className="mt-2 break-words text-xs">{item.issues.join(" / ") || "等待人工画面确认"}</p><p className="mt-2 break-all text-[10px] text-muted-foreground">{item.id} · {item.asset_id}</p></Link>)}</div>}</Panel>
      <Panel title="操作历史">{audits.length?<ol className="space-y-4">{[...audits].reverse().slice(0,15).map(item=><li key={item.id}><p className="text-xs leading-6">{item.message}</p><p className="mt-1 text-[10px] text-muted-foreground">{timeLabel(item.created_at)} · {item.action}</p></li>)}</ol>:<p className="text-xs text-muted-foreground">尚无操作历史。</p>}</Panel>
    </div><aside className="min-w-0 space-y-6">
      <Panel title="任务资料"><dl className="space-y-4 text-xs">{[["国家",task.country],["Keyword",task.keyword],["产品",task.product_id],["原始视频",task.source_video_id],["来源文档",context.source_document],["来源行",context.source_row],["生产状态",task.status]].map(([label,value])=><div key={String(label)}><dt className="text-[10px] text-muted-foreground">{String(label)}</dt><dd className="mt-1 break-all leading-6">{display(value)}</dd></div>)}</dl><Button className="mt-5 w-full" variant="outline" onClick={exportMetadata}><Download size={13}/>导出任务记录</Button><p className="mt-3 break-all text-[10px] text-muted-foreground">母资产：{display(production.current_person_asset_id)}</p></Panel>
      <WorkflowHistory taskId={task.id}/>
      <Panel title="进程监督" description="实际执行记录；心跳不是质量或完成证明。">{latestRun?<dl className="space-y-3 text-[11px]">{[["Run",latestRun.id],["Provider",latestRun.provider],["PID",latestRun.pid],["状态",latestRun.status],["开始",latestRun.started_at?timeLabel(latestRun.started_at):null],["最近心跳",latestRun.last_heartbeat?timeLabel(latestRun.last_heartbeat):null],["退出码",latestRun.exit_code],["终止信号",latestRun.termination_signal],["超时",latestRun.timed_out===undefined?null:latestRun.timed_out?"是":"否"],["失败原因",latestRun.error_code]].map(([label,value])=><div key={String(label)}><dt className="text-[10px] text-muted-foreground">{String(label)}</dt><dd className="mt-1 break-all">{display(value)}</dd></div>)}</dl>:<p className="text-xs text-muted-foreground">尚无真实执行记录。</p>}</Panel>
      <Panel title="异常" description={`${errors.length} 条记录`}>{errors.length?<div className="space-y-4">{[...errors].reverse().map(error=><div key={error.id}><p className="break-words text-xs text-red-600">{error.code}</p><p className="mt-2 break-words text-[11px] leading-6">{error.message}</p><p className="mt-1 text-[9px] text-muted-foreground">{error.retryable?"可重试":"需检查后处理"} · {timeLabel(error.created_at)}</p></div>)}</div>:<p className="text-xs text-muted-foreground">暂无异常记录。</p>}</Panel>
    </aside></div>
  </>;
}
