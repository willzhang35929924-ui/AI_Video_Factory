"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Panel, StatusBadge } from "@/components/factory-ui";
import { request } from "@/lib/api";

type Policy = {enabled:boolean;safe_pre_submit_retry_limit:number;retry_backoff_seconds:number};
type TriageItem = {task_id:string;video_id:string|null;stage:string;previous_status:string;next_status:string;original_reason:string;decision:string;reason:string;eligible:boolean};
type TriagePlan = {plan_id:string;fingerprint:string;total:number;eligible:number;summary:Record<string,number>;items:TriageItem[];paid_requests_submitted:number};
type TriageReceipt = {summary:Record<string,number>;paid_requests_submitted:number;batch_resumed:boolean;receipt_path:string};

export function useUnattendedPolicy() {
  return useQuery({queryKey:["collector-unattended-policy"],queryFn:()=>request<Policy>("/api/collector/unattended-policy"),retry:false});
}

export function CollectorUnattendedPanel() {
  const policy=useUnattendedPolicy();
  const client=useQueryClient();
  const [plan,setPlan]=useState<TriagePlan|null>(null);
  const [receipt,setReceipt]=useState<TriageReceipt|null>(null);
  const [confirmed,setConfirmed]=useState(false);
  const [error,setError]=useState("");
  const save=useMutation({mutationFn:(enabled:boolean)=>request<Policy>("/api/collector/unattended-policy",{method:"PUT",body:JSON.stringify({enabled,safe_pre_submit_retry_limit:policy.data!.safe_pre_submit_retry_limit,retry_backoff_seconds:policy.data!.retry_backoff_seconds})}),onSuccess:value=>{client.setQueryData(["collector-unattended-policy"],value);setPlan(null);setReceipt(null);setConfirmed(false);setError("");},onError:exc=>setError(exc.message)});
  const prepare=useMutation({mutationFn:()=>request<TriagePlan>("/api/collector/auto-triage/plan",{method:"POST"}),onSuccess:value=>{setPlan(value);setReceipt(null);setConfirmed(false);setError("");},onError:exc=>setError(exc.message)});
  const apply=useMutation({mutationFn:()=>request<TriageReceipt>("/api/collector/auto-triage/apply",{method:"POST",body:JSON.stringify({plan_id:plan!.plan_id,fingerprint:plan!.fingerprint,confirmed:true})}),onSuccess:async value=>{setReceipt(value);setConfirmed(false);setError("");await client.invalidateQueries({queryKey:["factory"]});await client.invalidateQueries({queryKey:["production-queue"]});},onError:exc=>{setError(exc.message);setConfirmed(false);}});
  const busy=save.isPending || prepare.isPending || apply.isPending;
  return <Panel title="无人值守采集" description="机器决定通过、拒绝或隔离；不确定不是通过，成片人工审核仍独立保留。">
    {policy.isError?<p role="alert" className="text-xs text-red-600">策略读取失败：{policy.error.message}；未假定策略启用。</p>:policy.isPending?<p role="status" className="text-xs">正在读取已保存策略…</p>:<div className="flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-2 text-xs"><input type="checkbox" aria-label="启用无人值守采集" checked={policy.data.enabled} disabled={busy} onChange={event=>save.mutate(event.target.checked)}/>启用无人值守采集</label>
      <span className="text-[10px] text-muted-foreground">仅确认尚未提交的网络故障最多重试 {policy.data.safe_pre_submit_retry_limit} 次；未知请求不重交。</span>
      <Button size="sm" variant="outline" disabled={busy || !policy.data.enabled} onClick={()=>prepare.mutate()}>{prepare.isPending?"核对已有证据…":"预览历史自动归类"}</Button>
    </div>}
    <p className="mt-3 text-[10px] leading-6 text-muted-foreground">本操作新增付费请求为 0。只归类采集阶段的历史等待，不批准质检、不恢复暂停/取消批次，不重新采集、下载或生成背景。</p>
    {error && <p role="alert" className="mt-3 text-xs text-red-600">{error}</p>}
    {plan && <div className="mt-4 space-y-4 border-t pt-4">
      <p role="status" className="text-xs">检查 {plan.total} 条等待记录；可归类采集 {plan.eligible} 条；下游审核或活动任务 {plan.total-plan.eligible} 条保持不变。</p>
      <div className="flex flex-wrap gap-3">{Object.entries(plan.summary).map(([status,count])=><span key={status} className="text-xs"><StatusBadge status={status}/> {count}</span>)}</div>
      <details className="text-xs"><summary className="cursor-pointer">逐条 Task / 原因 / 拟转移状态</summary><div className="mt-3 max-h-72 space-y-3 overflow-y-auto">{plan.items.map(item=><div key={item.task_id} className="border-b pb-3"><Link href={`/tasks/${item.task_id}`} className="break-all font-mono text-[10px] underline">{item.task_id}</Link><p className="mt-1">{item.video_id || "无视频 ID"} · {item.stage} · {item.previous_status} → {item.next_status}</p><p className="mt-1 break-words text-muted-foreground">{item.original_reason} → {item.reason}</p></div>)}</div></details>
      {receipt?<div role="status" className="text-xs leading-6">已逐条保存：{Object.entries(receipt.summary).map(([key,count])=>`${key} ${count}`).join("；")}。新增请求 {receipt.paid_requests_submitted}；恢复批次 {receipt.batch_resumed?"是":"否"}。<p className="break-all font-mono text-[9px]">{receipt.receipt_path}</p></div>:<>
        <label className="flex items-start gap-2 text-xs leading-6"><input type="checkbox" aria-label="确认历史自动归类范围" checked={confirmed} disabled={busy} onChange={event=>setConfirmed(event.target.checked)}/>确认按上述证据归类；不确定项隔离而不是批准，历史结果与下游审核保留。</label>
        <Button size="sm" disabled={busy || !confirmed || !plan.eligible} onClick={()=>apply.mutate()}>{apply.isPending?"逐条复核并保存…":"应用历史自动归类"}</Button>
      </>}
    </div>}
  </Panel>;
}
