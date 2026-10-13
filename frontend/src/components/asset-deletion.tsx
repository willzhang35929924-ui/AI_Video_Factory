"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { request } from "@/lib/api";
import { type Asset } from "@/lib/types";

export type AssetDeletionStatus = {
  asset_id: string; can_delete: boolean; status: string; history_version: boolean; fingerprint: string;
  bytes_to_release: number; allocated_bytes_reclaim_estimate: number; reasons: string[]; warnings: string[]; references: {type:string;id:string;reason?:string}[];
  requires_impact_confirmation: boolean; affected_task_ids: string[];
  files: {path:string;exists:boolean;size:number;allocated_bytes:number;link_count:number;shared:boolean;action:"DELETE"|"KEEP_SHARED"}[];
};

export type AssetFamilyDeletionPlan = {
  requested_asset_ids:string[]; expanded_asset_ids:string[]; assets:(Asset & {delete_protection?:string[]})[];
  edges:{from_asset_id:string;to_asset_id:string;reason:string}[];
  references:{type:string;id:string;reason?:string}[];
  files:(AssetDeletionStatus["files"][number] & {asset_ids?:string[];external_registered_media?:boolean;sha256?:string})[]; file_count:number; bytes_to_release:number;
  records:{table:string;id:string;action:string;reason:string}[]; record_actions:Record<string,number>;
  affected_task_ids:string[]; remaining_external_media:{asset_id:string;path:string;reason:string}[]; published_anchor_ids:string[];
  allocated_bytes_reclaim_estimate:number; retained_shared_bytes:number;
  can_delete:boolean; reasons:string[]; warnings:string[]; requires_impact_confirmation:boolean; fingerprint:string;
  orphan_outputs?:{source_asset_id:string;deleted_person_asset_id:string;deletion_audit_id:string;processing_run_id:string;output_path:string;output_sha256:string;manifest_path:string;directory:string;task_id:string}[];
};

export function assetFamilyPlan(assetIds:string[],scope:string) {
  return request<AssetFamilyDeletionPlan>("/api/assets/family-deletion-plan",{method:"POST",body:JSON.stringify({asset_ids:[...new Set(assetIds)].sort(),scope}),signal:AbortSignal.timeout(30000)});
}

export function fileSize(bytes:number|null|undefined) {
  if(bytes==null)return "大小未知";
  if(bytes<1024)return `${bytes} B`;
  if(bytes<1024**2)return `${(bytes/1024).toFixed(1)} KB`;
  return `${(bytes/1024**2).toFixed(1)} MB`;
}

const protectionLabels:Record<string,string>={UNREFERENCED:"未被引用 · 可以删除",REFERENCED:"历史/任务引用 · 确认影响后可删除",RUNNING:"相关任务正在运行 · 等待安全边界后重试",FILE_IN_USE:"文件仍被进程写入 · 等待完成后重试",FILE_LOCK_CHECK_FAILED:"无法核实文件占用 · 请重新检查",ACTIVE_PRODUCTION:"关联生产队列 · 确认后可删除",CURRENT_PERSON_ASSET:"当前人像母资产 · 确认后可删除",CURRENT_COMPOSITION_ASSET:"当前合成或成片 · 确认后可删除",HISTORY_VERSION:"历史版本 · 确认后可删除",EXTERNAL_LEGACY_FILE:"旧系统或外部文件只读，不能删除",SHARED_FILE:"共享文件保留 · 仅删除选中素材记录与专属文件",SYMLINK_FILE_PROTECTED:"符号链接文件禁止删除",PROTECTED_TECHNICAL_ASSET:"技术验证素材 · 确认后可删除",PROTECTED_HISTORY_RECORD:"历史登记 · 确认后可删除",MEDIA_DELETED:"媒体此前已删除"};
export function deletionLabel(status:string){return protectionLabels[status]||status;}

export type DeleteResult={asset_id:string;status:string;deleted_asset_ids?:string[];record_deleted?:boolean;released_bytes?:number;retained_shared_bytes?:number;deleted_allocated_bytes?:number;disk_free_before?:number;disk_free_after?:number;message?:string;code?:string;cleanup_errors?:{path:string;error:string}[];published_anchor_ids?:string[];audit_id?:string;original_files_absent?:boolean;shared_files_preserved?:boolean};
const recordActionLabels:Record<string,string>={DELETE:"删除记录",DETACH:"保留记录并清除已删素材引用",KEEP_RECEIPT:"保留真实发布回执",KEEP_RECEIPT_ANCHOR:"保留发布回执关联锚点"};

export function AssetFamilyDeletionPreview({plan,loading,error}:{plan?:AssetFamilyDeletionPlan;loading?:boolean;error?:string}) {
  if(loading)return <p role="status" className="text-xs leading-6">正在核对关联素材、真实文件与删除保护…</p>;
  if(error)return <p role="alert" className="text-xs leading-6 text-red-600 dark:text-red-400">关联删除计划未确认：{error}。尚未执行删除，请重新检查。</p>;
  if(!plan)return null;
  return <div className="space-y-3 text-xs">
    <div role="status" className="rounded-lg border p-3 leading-6">选择 {plan.requested_asset_ids.length} 项 · 自动关联 {plan.expanded_asset_ids.length-plan.requested_asset_ids.length} 项 · 共 {plan.expanded_asset_ids.length} 项 · 待删除文件 {plan.file_count} 个
      <p>待删除留存 {fileSize(plan.bytes_to_release)} · 预计释放文件块 {fileSize(plan.allocated_bytes_reclaim_estimate)} · 保留共享文件 {fileSize(plan.retained_shared_bytes)}</p>
      <p className="text-muted-foreground">只关联数据库中明确记录的来源、版本与下游产物，不按国家、关键词或同一任务猜测。实际空间按删除后磁盘统计更新。</p>
    </div>
    {plan.reasons.length>0&&<p role="alert" className="rounded-lg border p-3 leading-6 text-red-600 dark:text-red-400">当前无法删除：{plan.reasons.map(deletionLabel).join("；")}。不会终止运行或删除受保护路径。</p>}
    {plan.warnings.length>0&&<p className="leading-6 text-amber-700 dark:text-amber-400">{plan.warnings.map(warning=>warning==="SHARED_FILE"&&!plan.files.some(file=>file.action==="KEEP_SHARED")?"共享依赖已联动纳入本次删除":deletionLabel(warning)).join("；")}</p>}
    {plan.affected_task_ids.length>0&&<div className="rounded-lg border p-3 leading-6 text-amber-700 dark:text-amber-400">受影响任务将取消后续自动生产，防止重新生成已删除素材；不会终止运行中的进程。{plan.affected_task_ids.map(id=><p key={id} className="break-all font-mono text-[10px]">{id}</p>)}</div>}
    {plan.published_anchor_ids.length>0&&<p className="leading-6 text-muted-foreground">本地媒体可删除，已发布内容不会删除；真实发布回执和必要关联锚点保留，本次结果会标记 PARTIAL。</p>}
    <details open className="rounded-lg border p-3"><summary className="cursor-pointer">联动素材与文件（{plan.expanded_asset_ids.length} 项）</summary><div className="mt-3 max-h-80 space-y-3 overflow-y-auto">{plan.expanded_asset_ids.map(id=>{const asset=plan.assets.find(row=>row.id===id);return <div key={id} className="border-t pt-3"><p>{plan.requested_asset_ids.includes(id)?"手动选择":"自动关联"} · {asset?.name||"已登记素材"} · {asset?.asset_type||asset?.type||"UNKNOWN"}</p><p className="mt-1 break-all font-mono text-[10px]">{id}</p>{asset&&(asset.external_path||asset.storage_path)&&<p className="mt-1 break-all font-mono text-[10px] text-muted-foreground">{asset.external_path||asset.storage_path}</p>}</div>;})}{plan.files.map(file=><div key={file.path} className="break-all border-t pt-3 font-mono text-[10px]"><p>{file.action==="KEEP_SHARED"?"保留共享文件":"删除文件"} · {file.external_registered_media?"外部已登记原片 · 将真实删除":"Factory 文件"} · {file.exists?fileSize(file.size):"媒体不存在"} · {file.path}</p>{file.sha256 && <p className="mt-1 text-muted-foreground">SHA-256：{file.sha256}</p>}</div>)}</div></details>
    {!!plan.orphan_outputs?.length&&<details open className="rounded-lg border p-3"><summary className="cursor-pointer">历史已删版本的残留预览（{plan.orphan_outputs.length}）</summary><p className="mt-3 leading-6 text-muted-foreground">按来源路径与 Hash、历史删除审计和输出清单核实；未重新创建已删除的素材记录。</p>{plan.orphan_outputs.map(row=><div key={row.output_path} className="mt-3 space-y-1 break-all font-mono text-[10px]"><p>来源：{row.source_asset_id} → 已删人像：{row.deleted_person_asset_id}</p><p>审计：{row.deletion_audit_id} · Run：{row.processing_run_id}</p><p>{row.output_path}</p><p>SHA-256：{row.output_sha256}</p></div>)}</details>}
    {plan.edges.length>0&&<details className="rounded-lg border p-3"><summary className="cursor-pointer">明确的关联路径（{plan.edges.length}）</summary>{plan.edges.map((edge,index)=><p key={`${edge.from_asset_id}-${edge.to_asset_id}-${index}`} className="mt-3 break-all font-mono text-[10px]">{edge.from_asset_id} → {edge.to_asset_id} · {edge.reason}</p>)}</details>}
    {plan.references.length>0&&<details className="rounded-lg border p-3"><summary className="cursor-pointer">关联 Task / Run / Review 等记录（{plan.references.length}）</summary>{plan.references.map((reference,index)=><p key={`${reference.type}-${reference.id}-${index}`} className="mt-3 break-all font-mono text-[10px]">{reference.type} · {reference.id} · {reference.reason&&deletionLabel(reference.reason)}</p>)}</details>}
    <details open className="rounded-lg border p-3"><summary className="cursor-pointer">关联记录删除 / 保留明细（{plan.records.length}）</summary><p className="mt-3 leading-6 text-muted-foreground">以下标记为“删除”的 Run、Review、质量、合成与工作流记录会一并删除。未关联任务和删除审计保留；非独占运行或外部请求记录只清除已删素材引用。</p><div className="mt-3 max-h-64 space-y-2 overflow-y-auto">{plan.records.map(record=><p key={`${record.table}-${record.id}`} className="break-all font-mono text-[10px]">{recordActionLabels[record.action]||record.action} · {record.table} · {record.id} · {record.reason}</p>)}</div></details>
    {plan.remaining_external_media.length>0&&<details className="rounded-lg border p-3"><summary className="cursor-pointer">保留的旧系统 / 外部原片（{plan.remaining_external_media.length}）</summary>{plan.remaining_external_media.map(row=><p key={`${row.asset_id}-${row.path}`} className="mt-3 break-all font-mono text-[10px]">{row.asset_id} · {row.path} · {row.reason}</p>)}</details>}
  </div>;
}

export function AssetDeletionDialog({assets,scope="production",onDeleted,label="删除素材",disabled=false}:{assets:Asset[];scope?:string;onDeleted?:(results:DeleteResult[])=>void;label?:string;disabled?:boolean}) {
  const client=useQueryClient();
  const [open,setOpen]=useState(false);
  const [confirmed,setConfirmed]=useState("");
  const [working,setWorking]=useState(false);
  const [error,setError]=useState("");
  const [results,setResults]=useState<DeleteResult[]>([]);
  const [dialogAssets,setDialogAssets]=useState<Asset[]>([]);
  const selectedIds=dialogAssets.map(asset=>asset.id).sort();
  const status=useQuery({queryKey:["asset-family-deletion-plan",scope,selectedIds],queryFn:()=>assetFamilyPlan(selectedIds,scope),enabled:open&&!results.length&&!!selectedIds.length,retry:false,refetchOnWindowFocus:false});
  const plan=status.data;
  async function remove(){
    if(!plan?.can_delete||confirmed!==plan.fingerprint||!plan.expanded_asset_ids.length||status.isFetching)return;
    setWorking(true);setError("");setResults([]);
    try{
      const response=await request<Omit<DeleteResult,"asset_id">>("/api/assets/family-delete",{method:"POST",body:JSON.stringify({asset_ids:selectedIds,scope,fingerprint:plan.fingerprint,confirmed:true,impact_confirmed:true}),signal:AbortSignal.timeout(120000)});
      const deleted=[{...response,asset_id:selectedIds[0]}];
      setResults(deleted);setConfirmed("");
      onDeleted?.(deleted);
      void Promise.all([client.invalidateQueries({queryKey:["asset-registry"]}),client.invalidateQueries({queryKey:["asset-media-status"]}),client.invalidateQueries({queryKey:["asset-storage-usage"]}),client.invalidateQueries({queryKey:["asset-record"]}),client.invalidateQueries({queryKey:["asset-family-deletion-plan"],refetchType:"none"}),client.invalidateQueries({queryKey:["factory"]})]).catch(()=>setError("删除结果已确认，但列表刷新失败；请关闭后刷新素材。"));
    }catch(caught){setError(caught instanceof Error?caught.message:"删除失败，文件没有确认删除");setConfirmed("");void status.refetch();}
    finally{setWorking(false);}
  }
  return <><Button size="sm" variant="outline" disabled={disabled||!assets.length} onClick={()=>{setDialogAssets(assets);setConfirmed("");setError("");setResults([]);setOpen(true);}}><Trash2 size={13}/>{label}</Button>
    <Dialog open={open} onOpenChange={value=>{if(!working)setOpen(value);}}><DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
      <DialogHeader><DialogTitle>删除素材及关联产物</DialogTitle><DialogDescription>根据真实关联联动删除所列素材、媒体、预览缓存和历史记录，包括明确登记的外部原片。请核对每个精确路径；未关联文件、删除审计与外部发布回执保留。此操作无法从界面撤销。</DialogDescription></DialogHeader>
      {status.isPending&&!results.length?<p role="status" className="text-xs">正在核对文件与引用关系…</p>:status.isError&&!results.length?<p role="alert" className="text-xs text-red-600">{status.error.message}</p>:<>
        <AssetFamilyDeletionPreview plan={plan}/>
        {results.length>0&&<div role="status" className="space-y-2 rounded-lg border p-3 text-xs">{results.map(result=><div key={result.asset_id} className="break-all">{["DELETED","PARTIAL"].includes(result.status)?`已删除 ${result.deleted_asset_ids?.length??1} 项素材记录；删除文件留存 ${fileSize(result.released_bytes)}，移除文件块 ${fileSize(result.deleted_allocated_bytes??0)}，保留共享文件 ${fileSize(result.retained_shared_bytes??0)}；磁盘可用变化 ${result.disk_free_before!=null&&result.disk_free_after!=null?`${result.disk_free_after>=result.disk_free_before?"+":"-"}${fileSize(Math.abs(result.disk_free_after-result.disk_free_before))}`:"未知"}`:result.status==="DELETED_WITH_CLEANUP_ERROR"?"素材记录与原路径已移除，但隔离文件清理失败；尚未全部释放空间，请检查下方路径":`删除失败：${result.message||result.code||result.status}`}{result.status==="PARTIAL"&&<p className="mt-2 text-amber-700 dark:text-amber-400">PARTIAL：本地家族已删除，已发布内容的真实回执和关联锚点保留；未删除外部公开内容。</p>}<p className="mt-2 font-mono text-[10px]">{result.deleted_asset_ids?.join(" · ")}</p><p className="mt-2 text-[10px]">原媒体路径：{result.original_files_absent===true?"已核实不存在":result.original_files_absent===false?"仍有文件，未全部释放":"未返回核验结果"} · 共享文件：{result.shared_files_preserved===true?"已核实保留":result.shared_files_preserved===false?"核验失败":"未返回核验结果"}</p>{result.audit_id&&<p className="mt-2 break-all font-mono text-[10px]">删除审计：{result.audit_id}</p>}{result.cleanup_errors?.map(item=><p key={item.path} className="mt-1">{item.error} · {item.path}</p>)}</div>)}</div>}
        {error&&<p role="alert" className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        {plan?.can_delete&&plan.expanded_asset_ids.length>0&&!results.length?<>
          <label className="flex items-start gap-3 text-xs leading-6"><input aria-label="确认永久删除媒体" type="checkbox" className="mt-1" checked={confirmed===plan.fingerprint} disabled={working||status.isFetching} onChange={event=>setConfirmed(event.target.checked?plan.fingerprint:"")}/>我已核对全部联动素材、文件及记录删除明细，确认永久删除 {plan.expanded_asset_ids.length} 项素材及所列历史记录，并取消受影响任务的后续生产；无法从此页面撤销。</label>
          <Button disabled={confirmed!==plan.fingerprint||working||status.isFetching} onClick={remove}>{working?"正在删除并验证文件…":`确认联动删除 ${plan.expanded_asset_ids.length} 项`}</Button>
        </>:!results.length?<p className="text-xs leading-6 text-muted-foreground">未执行删除；请查看阻塞原因，等待运行完成或修复路径后重新检查。</p>:null}
      </>}
      <div className="flex gap-2"><Button variant="outline" disabled={working||status.isFetching||!!results.length} onClick={()=>void status.refetch()}>重新检查</Button><Button variant="outline" disabled={working} onClick={()=>setOpen(false)}>关闭</Button></div>
    </DialogContent></Dialog></>;
}
