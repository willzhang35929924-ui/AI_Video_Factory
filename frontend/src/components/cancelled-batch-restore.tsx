"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useNotice } from "@/components/providers";
import { request } from "@/lib/api";

type RestorePlan = {
  batch_id:string; status:string; fingerprint:string; eligible_count:number; eligible_task_ids:string[];
  preserved_count:number; excluded:{task_id:string;status:string;reason:string}[];
  status_counts:Record<string,number>; can_restore:boolean; message:string;
};

const preservationReasons:Record<string,string> = {
  HISTORICAL_RESULT_PRESERVED:"已处理结果保留", NOT_UNSTARTED_COLLECTOR:"不是未开始采集项",
  PRIOR_RUN_PRESERVED:"已有执行/外部请求记录", PRIOR_OUTPUT_PRESERVED:"已有结果或资产",
  CONTEXT_NOT_ELIGIBLE:"原任务上下文不符合恢复条件", PRIOR_STAGE_PRESERVED:"阶段曾执行",
  PRIOR_FILES_PRESERVED:"已存在文件或请求 journal", OUTPUT_STATE_UNVERIFIED:"无法核实文件状态",
};

export function CancelledBatchRestore({batchId,status,disabled=false}:{batchId?:string;status?:string;disabled?:boolean}) {
  const client = useQueryClient();
  const notice = useNotice();
  const [open,setOpen] = useState(false);
  const [confirmed,setConfirmed] = useState(false);
  const [working,setWorking] = useState(false);
  const [error,setError] = useState("");
  const path = `/api/production/batches/${encodeURIComponent(batchId || "")}/restore-cancelled`;
  const plan = useQuery({queryKey:["cancelled-batch-restore",batchId],queryFn:()=>request<RestorePlan>(path),
    enabled:open&&status==="CANCELLED"&&!!batchId,retry:false,refetchOnWindowFocus:false});
  async function restore() {
    if(!plan.data?.can_restore||!confirmed||working)return;
    setWorking(true);setError("");
    try {
      const result = await request<{message:string;restored_count:number;batch_id:string}>(path,{method:"POST",
        body:JSON.stringify({confirmed:true,fingerprint:plan.data.fingerprint}),signal:AbortSignal.timeout(120000)});
      notice(result.message);setOpen(false);setConfirmed(false);
      await Promise.all([client.invalidateQueries({queryKey:["factory"]}),client.invalidateQueries({queryKey:["production-queue"]}),
        client.invalidateQueries({queryKey:["collector-board"]})]);
    } catch(caught) {
      setError(caught instanceof Error?caught.message:"恢复未成功；请刷新查看原批次状态");
      setConfirmed(false);void plan.refetch();
    } finally {setWorking(false);}
  }
  if(status!=="CANCELLED"||!batchId)return null;
  return <><Button size="sm" variant="outline" disabled={disabled} onClick={()=>{setConfirmed(false);setError("");setOpen(true);void plan.refetch();}}>
    <RotateCcw size={13}/>恢复未开始的已取消任务
  </Button><Dialog open={open} onOpenChange={value=>{if(!working)setOpen(value);}}>
    <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-lg" showCloseButton={!working}>
      <DialogHeader><DialogTitle>恢复原批次未开始任务</DialogTitle><DialogDescription>这不是普通“继续”。只恢复从未执行、无结果、无文件的已取消采集项；不新建批次，不重放已付费分析或未知请求。</DialogDescription></DialogHeader>
      <p className="break-all font-mono text-[10px] text-muted-foreground">{batchId}</p>
      {plan.isFetching?<p role="status" className="text-xs">正在核对原取消状态、Run、阶段和文件…</p>:plan.isError?
        <p role="alert" className="text-xs text-red-600">{plan.error.message}</p>:plan.data&&<>
        <div className="rounded-lg border p-3 text-xs leading-6">可恢复 {plan.data.eligible_count} 项 · 保留 {plan.data.preserved_count} 项
          <p className="text-muted-foreground">{Object.entries(plan.data.status_counts).map(([key,count])=>`${key} ${count}`).join(" · ")}</p>
        </div>
        <p className="text-xs leading-6 text-muted-foreground">确认后重新检查真实采集依赖，在同一个批次串行执行这些未开始任务。拒绝、等待审核、历史 Run、已产生文件及取消记录保留。</p>
        <details className="text-xs"><summary className="cursor-pointer">查看恢复任务（{plan.data.eligible_count}）</summary>
          <div className="mt-2 max-h-36 overflow-y-auto font-mono text-[10px]">{plan.data.eligible_task_ids.map(id=><p key={id}>{id}</p>)}</div>
        </details>
        {plan.data.excluded.length>0&&<details className="text-xs"><summary className="cursor-pointer">保留、不重跑（{plan.data.preserved_count}）</summary>
          <div className="mt-2 max-h-36 space-y-2 overflow-y-auto text-[10px]">{plan.data.excluded.map(item=><p className="break-all" key={item.task_id}>{item.task_id} · {item.status} · {preservationReasons[item.reason] || item.reason}</p>)}</div>
        </details>}
        <label className="flex items-start gap-3 text-xs leading-6"><input aria-label="确认恢复未开始任务" type="checkbox" className="mt-1" checked={confirmed} disabled={working||!plan.data.can_restore} onChange={event=>setConfirmed(event.target.checked)}/>我确认恢复以上未开始任务，并沿用原任务规则开始真实采集和必要分析。</label>
        <Button disabled={!confirmed||!plan.data.can_restore||working||plan.isFetching} onClick={restore}>{working?"正在检查依赖并恢复…":`确认恢复并执行 ${plan.data.eligible_count} 项`}</Button>
      </>}
      {error&&<p role="alert" className="text-xs text-red-600">{error}</p>}
      <Button variant="outline" disabled={working} onClick={()=>setOpen(false)}>关闭</Button>
    </DialogContent>
  </Dialog></>;
}
