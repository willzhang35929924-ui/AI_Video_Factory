"use client";

import Link from "next/link";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Check, Copy, ExternalLink, RefreshCw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { StatusBadge } from "@/components/factory-ui";
import { useNotice } from "@/components/providers";
import { request } from "@/lib/api";
import { type ProcessingRun, type Task, timeLabel } from "@/lib/types";

type Candidate = {task_id:string;country:string;keyword:string;platform:string;video_id:string|null;video_url:string|null;video_url_error:string|null;title:string|null;author:string|null;task_status:string;state:string;candidate_decision:string;wait_reason:string;next_step:string;reviewable:boolean;review_unavailable_reason:string|null;fingerprint:string;updated_at:string;result:Record<string,unknown>;batch_ids:string[];runs:{id:string;status:string;pid:number|null;error_code:string|null;error_message:string|null}[];history:{id:string;decision:string;reason:string;operator:string;decided_at:string;waiting_state:string}[]};
const value=(item:unknown)=>item===undefined || item===null || item===""?"UNKNOWN":typeof item==="boolean"?item?"有":"无":String(item);

type CollectionIdentity = {status:"CANONICAL"|"DUPLICATE_SKIPPED";canonical_task_id:string;source_identity_key:string;duplicate_encounters?:number};

export function collectionIdentity(task:Task) {
  return task.context.collection_identity as CollectionIdentity | undefined;
}

export function collectorDuplicate(task:Task) {
  return collectionIdentity(task)?.status==="DUPLICATE_SKIPPED";
}

export function CollectorIdentityNotice({task}:{task:Task}) {
  const identity=collectionIdentity(task);
  if(!identity)return null;
  return <div className="mt-2 text-[10px] leading-5 text-muted-foreground">{collectorDuplicate(task)?<><p>历史重复 · 不新增有效采集，不重复分析或审核</p><Link href={`/tasks/${identity.canonical_task_id}`} className="block break-all underline">查看既有采集：{identity.canonical_task_id}</Link></>:<p>历史身份已归一{typeof identity.duplicate_encounters==="number"?` · 重复遇见 ${identity.duplicate_encounters} 次`:""}</p>}<details><summary className="cursor-pointer">视频身份键</summary><p className="break-all font-mono text-[9px]">{identity.source_identity_key}</p></details></div>;
}

export function candidateReviewable(task:Task) {
  const context=(task.context.task_context || {}) as Record<string,unknown>;
  return !collectorDuplicate(task) && task.status==="WAITING_REVIEW" && task.collector_result.decision_reason!=="watermark_unverified" && Boolean(task.collector_result.video_id || context.video_id);
}

export function candidateState(task:Task,runs:ProcessingRun[] = []) {
  if(collectorDuplicate(task))return "DUPLICATE_SKIPPED";
  const triage=(task.context.collector_auto_triage || {}) as Record<string,unknown>;
  if(triage.decision && ["COLLECTED","REJECTED","QUARANTINED","FAILED"].includes(task.status))return String(triage.decision);
  const saved=(task.context.collector_candidate_decision || {}) as Record<string,unknown>;
  if(saved.decision==="REJECTED")return "REJECTED";
  if(task.status!=="WAITING_REVIEW")return task.status;
  const code=task.collector_result.error_code || runs.findLast(run=>run.task_id===task.id && run.provider==="LegacyCollectorAdapter")?.error_code;
  return code==="EXTERNAL_BUDGET_EXHAUSTED"?"WAITING_EXTERNAL_BUDGET":code?"WAITING_MANUAL":task.status;
}

function CandidateInfo({candidate}:{candidate:Candidate}) {
  const notice=useNotice();
  return <div className="space-y-5">
    <div className="flex flex-wrap gap-2"><StatusBadge status={candidate.state}/><span className="text-xs">候选决定：{candidate.candidate_decision}</span></div>
    <p className="break-words text-xs leading-6">{candidate.country} · {candidate.keyword}<br/>{candidate.wait_reason}<br/><span className="text-muted-foreground">{candidate.next_step}</span></p>
    <dl className="grid grid-cols-2 gap-4 text-xs">{[["平台",candidate.platform],["标题",candidate.title],["作者",candidate.author],["视频 ID",candidate.video_id],["PV",candidate.result.views],["Like",candidate.result.likes],["赞播比",typeof candidate.result.like_view_ratio==="number"?`${candidate.result.like_view_ratio.toFixed(2)}%`:null],["AI匹配度",candidate.result.product_match_confidence],["AI来源",candidate.result.ai_origin],["抠像适用性",candidate.result.cutout_suitability],["贴片",candidate.result.overlay_detected],["水印",candidate.result.watermark_detected],["字幕遮挡",candidate.result.subtitle_detected],["旧采集决定",candidate.result.decision],["错误",candidate.result.error_code],["最近更新",timeLabel(candidate.updated_at)]].map(([label,entry])=><div key={String(label)}><dt className="text-[10px] text-muted-foreground">{String(label)}</dt><dd className="mt-1 break-words">{value(entry)}</dd></div>)}</dl>
    {candidate.video_url?<div className="space-y-3 border-t pt-4"><p className="break-all font-mono text-[10px]">{candidate.video_url}</p><div className="flex gap-2"><a href={candidate.video_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-xs"><ExternalLink size={13}/>打开原视频</a><Button size="sm" variant="outline" onClick={async()=>{try{await navigator.clipboard.writeText(candidate.video_url!);notice("真实视频链接已复制");}catch{notice("剪贴板权限不可用，请选择上方链接复制");}}}><Copy size={13}/>复制视频链接</Button></div></div>:<p role="alert" className="text-xs text-muted-foreground">{candidate.video_url_error}</p>}
    <details className="border-t pt-4 text-xs"><summary className="cursor-pointer">Task / Run / Batch 与历史审核决定</summary><Link className="mt-3 block break-all underline" href={`/tasks/${candidate.task_id}`}>{candidate.task_id}</Link><p className="mt-2 break-all text-[10px]">Batch：{candidate.batch_ids.join("、") || "无"}</p>{candidate.runs.map(run=><p key={run.id} className="mt-2 break-all text-[10px]">{run.id} · PID {run.pid ?? "UNKNOWN"} · {run.status} · {run.error_code || "无错误"}</p>)}{candidate.history.length?candidate.history.map(entry=><p key={entry.id} className="mt-3 text-xs leading-6">{entry.decision} · {entry.operator} · {timeLabel(entry.decided_at)}<br/>{entry.reason} · {entry.waiting_state}</p>):<p className="mt-3 text-muted-foreground">尚未提交候选人工决定。</p>}</details>
  </div>;
}

export function CollectorCandidateDrawer({task,label="查看详情"}:{task:Task;label?:string}) {
  const client=useQueryClient();const notice=useNotice();
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[candidate,setCandidate]=useState<Candidate|null>(null),[error,setError]=useState("");
  const [action,setAction]=useState<"accept"|"reject"|null>(null),[reason,setReason]=useState(""),[confirmed,setConfirmed]=useState(false);
  async function load(){setBusy(true);setError("");try{setCandidate(await request<Candidate>(`/api/collector/candidates/${encodeURIComponent(task.id)}`));}catch(exc){setError(exc instanceof Error?exc.message:"候选读取失败");}finally{setBusy(false);}}
  async function decide(){if(!candidate || !action || !confirmed || !reason.trim())return;setBusy(true);try{const result=await request<{candidate:Candidate;message:string}>(`/api/collector/candidates/${encodeURIComponent(task.id)}/decision`,{method:"POST",body:JSON.stringify({action,reason,fingerprint:candidate.fingerprint,confirmed:true})});setCandidate(result.candidate);setAction(null);setConfirmed(false);setReason("");await client.invalidateQueries({queryKey:["factory"]});notice(result.message);}catch(exc){setError(exc instanceof Error?exc.message:"决定保存失败");}finally{setBusy(false);}}
  if(collectorDuplicate(task))return <Link href={`/tasks/${collectionIdentity(task)!.canonical_task_id}`} className="inline-flex items-center rounded-lg border px-3 py-2 text-xs">查看历史采集</Link>;
  return <><Button size="sm" variant="ghost" onClick={()=>{setOpen(true);setAction(null);setConfirmed(false);void load();}}>{label}</Button><Dialog open={open} onOpenChange={setOpen}><DialogContent className="inset-y-0 left-auto right-0 top-0 h-dvh max-w-full grid-rows-[auto_minmax(0,1fr)] translate-x-0 translate-y-0 rounded-none p-6 sm:max-w-xl"><DialogHeader><DialogTitle>采集候选详情</DialogTitle><DialogDescription>查看真实来源与等待原因。候选意向、机器筛选、原片准入和成片审核独立保存。</DialogDescription></DialogHeader><div className="min-h-0 overflow-y-auto"><Button className="mb-4" size="sm" variant="outline" disabled={busy} onClick={()=>{setAction(null);setConfirmed(false);void load();}}><RefreshCw size={13}/>重新检查</Button>{busy && <p role="status" className="mb-3 text-xs">正在读取或保存后端记录…</p>}{error && <p role="alert" className="mb-4 text-xs text-red-600">{error}</p>}{candidate && <><CandidateInfo candidate={candidate}/><div className="mt-5 border-t pt-5">{candidate.reviewable?<><div className="flex gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={()=>{setAction("accept");setConfirmed(false);}}><Check size={13}/>同意采集</Button><Button size="sm" variant="outline" disabled={busy} onClick={()=>{setAction("reject");setConfirmed(false);}}><X size={13}/>拒绝采集</Button></div>{action && <div className="mt-4 space-y-3 rounded-lg border p-4"><p className="text-xs leading-6">{action==="accept"?"确认接受这一条候选意向？已完成阶段保留；外部额度、登录、分析和质量条件仍须真实满足。本操作不发送请求。":"确认终止这一条候选？仅停止后续调度，其他候选继续；所有历史和文件保留。"}</p><Textarea aria-label="候选审核理由" placeholder="填写本次决定的理由" value={reason} onChange={event=>setReason(event.target.value)} maxLength={2000}/><label className="flex items-start gap-2 text-xs"><input type="checkbox" checked={confirmed} onChange={event=>setConfirmed(event.target.checked)}/>我确认已查看此候选及等待原因；此决定不是质量通过或发布授权。</label><Button size="sm" disabled={busy || !confirmed || !reason.trim()} onClick={decide}>提交{action==="accept"?"同意":"拒绝"}决定</Button></div>}</>:<p className="text-xs leading-6 text-muted-foreground">{candidate.review_unavailable_reason}</p>}</div></>}</div></DialogContent></Dialog></>;
}

export function CollectorBatchReview({tasks,onSaved}:{tasks:Task[];onSaved:()=>void}) {
  const client=useQueryClient();const notice=useNotice();
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[action,setAction]=useState<"accept"|"reject">("accept"),[candidates,setCandidates]=useState<Candidate[]>([]),[error,setError]=useState(""),[reason,setReason]=useState(""),[confirmed,setConfirmed]=useState(false),[outcomes,setOutcomes]=useState<{task_id:string;status:string;error?:string}[]>([]);
  async function prepare(selected:"accept"|"reject"){setOpen(true);setAction(selected);setBusy(true);setConfirmed(false);setOutcomes([]);setError("");try{const records=await Promise.all(tasks.map(task=>request<Candidate>(`/api/collector/candidates/${encodeURIComponent(task.id)}`)));setCandidates(records);}catch(exc){setError(exc instanceof Error?exc.message:"批量候选读取失败");}finally{setBusy(false);}}
  async function save(){setBusy(true);try{const result=await request<{results:{task_id:string;status:string;error?:string}[];saved:number}>("/api/collector/candidates/decisions",{method:"POST",body:JSON.stringify({action,reason,confirmed:true,candidates:Object.fromEntries(candidates.map(candidate=>[candidate.task_id,candidate.fingerprint]))})});setOutcomes(result.results);await client.invalidateQueries({queryKey:["factory"]});onSaved();notice(`已逐条保存 ${result.saved}/${result.results.length} 个候选决定；没有启动外部请求。`);setConfirmed(false);}catch(exc){setError(exc instanceof Error?exc.message:"批量审核保存失败");}finally{setBusy(false);}}
  return <><Button size="sm" variant="outline" disabled={!tasks.length || busy || tasks.length>100} onClick={()=>prepare("accept")}>批量同意{tasks.length?`（${tasks.length}）`:""}</Button><Button size="sm" variant="outline" disabled={!tasks.length || busy || tasks.length>100} onClick={()=>prepare("reject")}>批量拒绝</Button><Dialog open={open} onOpenChange={setOpen}><DialogContent className="sm:max-w-xl"><DialogHeader><DialogTitle>批量{action==="accept"?"同意":"拒绝"}采集候选</DialogTitle><DialogDescription>仅这 {tasks.length} 条选中候选；每条独立保存，一个失败不回滚其他结果。不调用分析、下载、质量通过或公开发布。</DialogDescription></DialogHeader><div className="max-h-[55vh] space-y-4 overflow-y-auto">{busy && <p role="status" className="text-xs">读取/保存真实候选记录…</p>}{error && <p role="alert" className="text-xs text-red-600">{error}</p>}{candidates.map(candidate=><div key={candidate.task_id} className="rounded-lg border p-3 text-xs"><p>{candidate.country} · {candidate.keyword} · {candidate.video_id}</p><p className="mt-1 break-all text-[10px]">{candidate.task_id}</p><p className="mt-2 text-muted-foreground">{candidate.state} · {candidate.wait_reason}</p>{!candidate.reviewable && <p className="mt-2 text-red-600">此条无法审核，将独立返回失败。</p>}</div>)}<p className="text-xs leading-6">{action==="accept"?"同意保存候选意向；额度耗尽仍等待外部额度，UNKNOWN 不会重复提交。":"拒绝只终止所选候选，保留Task、结果、Run和文件历史，不影响同批其他候选。"}</p><Textarea aria-label="批量审核理由" placeholder="本次决定理由" maxLength={2000} value={reason} onChange={event=>setReason(event.target.value)}/><label className="flex items-start gap-2 text-xs"><input type="checkbox" checked={confirmed} onChange={event=>setConfirmed(event.target.checked)}/>已核对上述范围与等待原因，确认提交逐条候选决定。</label>{outcomes.map(outcome=><p role="status" key={outcome.task_id} className="break-all text-[10px]">{outcome.task_id} · {outcome.status} {outcome.error || ""}</p>)}</div><Button disabled={busy || !!error || !candidates.length || !confirmed || !reason.trim()} onClick={save}>确认批量{action==="accept"?"同意":"拒绝"}</Button></DialogContent></Dialog></>;
}
