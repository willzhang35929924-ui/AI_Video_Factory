"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { request } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/factory-ui";

type SubmissionTask = { id:string; status:string; submission_state:string; current_step?:string; error?:string; platform_url?:string; checkpoint?:{execution_scope?:string;external_outcome?:string} };
type Confirmation = {
 status:string; fingerprint:string; caption:string;
 snapshot:{ publishing_task_id:string; source_task_id:string; account_id:string; username:string; country:string; environment_id:string;
 video_id:string; video_sha256:string; person_asset_id:string; composition_asset_id:string; product_id:string; selected_product_id:string;
 selected_product_name:string; description_id:string; description_sha256:string; content_review_sha256:string };
 content_review:Record<string,unknown>;
};
type Reconciliation = { status:string; read_only:boolean; task:SubmissionTask; observed?:{error_code?:string}; reused?:boolean };

export function publishGateHelp(code:string) {
 if (/APPROVED_FINAL|CURRENT_.*REVIEW|CURRENT_COMPOSITION_NOT_APPROVED|CURRENT_REVIEW_LINEAGE|SOURCE_TASK_NOT_PUBLISHABLE/.test(code)) return "请在审核中心审核当前人物版本对应的成片；旧版本通过不会放行新版本。不会代替你批准视频。";
 if (/DESCRIPTION_CONTENT_REVIEW/.test(code)) return "先核验所选产品的文案事实。系统不会自动把未经核验的功效或产品事实标记通过。";
 if (/OBSERVED_ACCOUNT|ACCOUNT_ENVIRONMENT/.test(code)) return "需核实此任务绑定的真实账号、HubStudio 环境和出口国家；其它已打开环境不能替代。";
 if (/CONTENT_REVIEW_EVIDENCE/.test(code)) return "需在当前账号上传页观察实际内容审查通过，并核对视频、文案和挂载商品。UNKNOWN 不会按通过处理。";
 if (/PUBLIC_SUBMISSION_DISABLED/.test(code)) return "此任务或配置仍为仅上传验证，尚未允许公开提交。已上传视频保留，不会重新上传。";
 if (/PUBLISH_CONTROL_NOT_OBSERVED/.test(code)) return "尚未观察到当前页面可用的 Publish 控件；未点击发布。完成页面准备后重新检查，不能使用其它账号的页面。";
 if (/RECEIPT|SUBMISSION_ALREADY|UNKNOWN|PUBLISH_RETRY_NOT_SAFE/.test(code)) return "提交结果未确认时只允许读取真实发布回执，禁止再次点击 Publish；上传成功不代表发布成功。";
 if (/CONFIRMATION_INPUT_CHANGED/.test(code)) return "视频、商品、账号、文案或审查证据发生变化，请重新读取确认信息；旧确认不会复用。";
 return "当前门槛未通过。已保存的任务、上传和历史步骤保留；请完成显示的真实依赖后重新检查。";
}

export function PublishingSubmitControl({taskId}:{taskId:string}) {
 const query=useQueryClient();
 const task=useQuery({queryKey:["publishing","submit-task",taskId],queryFn:()=>request<SubmissionTask>(`/api/publishing/tasks/${taskId}`),refetchInterval:3000});
 const [confirmation,setConfirmation]=useState<Confirmation|null>(null),[checked,setChecked]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(""),[message,setMessage]=useState("");
 const row=task.data, unresolved=!!row&&["SUBMITTING","SUBMITTED","UNKNOWN","UNKNOWN_EXTERNAL_RESULT"].includes(row.submission_state), unknown=row?.checkpoint?.external_outcome==="UNKNOWN";
 const running=!!row&&["QUEUED","RUNNING"].includes(row.status), verified=row?.submission_state==="VERIFIED", uploadOnly=row?.checkpoint?.execution_scope==="UPLOAD_ONLY";
 async function refresh(){await task.refetch();await query.invalidateQueries({queryKey:["publishing"]});}
 async function inspect(){setBusy(true);setError("");setMessage("");setChecked(false);setConfirmation(null);try{
  const result=await request<Confirmation>(`/api/publishing/tasks/${taskId}/submit-confirmation`,{signal:AbortSignal.timeout(120000)});
  if(result.status!=="READY_FOR_SINGLE_TASK_CONFIRMATION"||result.snapshot.publishing_task_id!==taskId) throw new Error("PUBLISH_CONFIRMATION_INPUT_CHANGED");
  setConfirmation(result);
 }catch(e){setError(e instanceof Error?e.message:"PUBLISH_CONFIRMATION_UNAVAILABLE");}finally{setBusy(false);}}
 async function submit(){if(!confirmation||!checked||running||unresolved||unknown||uploadOnly||verified)return;setBusy(true);setError("");setMessage("");try{
  const result=await request<{accepted:boolean;status:string;message:string}>(`/api/publishing/tasks/${taskId}/confirm-submit`,{method:"POST",body:JSON.stringify({fingerprint:confirmation.fingerprint,confirmed:true}),signal:AbortSignal.timeout(120000)});
  setMessage(`${result.status} · ${result.message}。这不是发布成功；请等待真实回执。`);await refresh();
 }catch(e){setError(e instanceof Error?e.message:"PUBLISH_CONFIRMATION_FAILED");}finally{setChecked(false);setConfirmation(null);setBusy(false);}}
 async function reconcile(){setBusy(true);setError("");setMessage("");try{
  const result=await request<Reconciliation>(`/api/publishing/tasks/${taskId}/reconcile-submit`,{method:"POST",signal:AbortSignal.timeout(120000)});
  setMessage(result.status==="PUBLISHED"&&result.task.submission_state==="VERIFIED"?"PUBLISHED · 后台已核对当前账号、视频、商品和文案的真实发布回执。":"WAITING_MANUAL · "+(result.observed?.error_code||"PUBLICATION_RECEIPT_NOT_VERIFIED")+"。未重复提交。");
  await refresh();
 }catch(e){setError(e instanceof Error?e.message:"PUBLICATION_RECONCILIATION_FAILED");}finally{setBusy(false);}}
 return <section className="my-4 space-y-3 rounded-lg border p-4" aria-label="单条正式发布确认">
  <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-medium">单条正式发布确认</h3><StatusBadge status={row?.submission_state||"UNKNOWN"}/></div>
  <p className="text-xs leading-6 text-muted-foreground">只确认此任务。当前 Final、人物版本、事实审核、账号、商品与内容审查全部通过后才允许提交；请求受理不等于已发布。</p>
  {task.error&&<p role="alert" className="text-xs text-red-600">{task.error.message}</p>}
  {row&&<p role="status" className="text-xs">{row.status} · {row.current_step||"PRECHECK"}{row.error?` · ${row.error}`:""}</p>}
  {uploadOnly&&<p className="text-xs text-muted-foreground">仅上传验证任务不公开发布；上传结果和历史步骤保留。</p>}
  {(unresolved||unknown)&&<p className="text-xs leading-6 text-muted-foreground">WAITING_MANUAL：已有外部提交或未知请求。只读核实回执，不重新上传、挂商品或再次点击 Publish。</p>}
  <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={busy||!row||running||unresolved||unknown||uploadOnly||verified} onClick={inspect}>读取单条发布确认</Button><Button size="sm" variant="outline" disabled={busy||!row||running||(!unresolved&&!verified)} onClick={reconcile}>只读核实发布回执</Button></div>
  {error&&<div role="alert" className="space-y-2 text-xs text-red-600"><p>WAITING_MANUAL · {error}</p><p className="leading-6 text-muted-foreground">{publishGateHelp(error)}</p><Link className="underline" href="/review">进入审核中心</Link></div>}
  {message&&<p role="status" className="text-xs leading-6">{message}</p>}
  {confirmation&&<div className="space-y-3 border-t pt-3 text-xs">
   <dl className="grid gap-2 sm:grid-cols-2">{[["账号 / 环境",`${confirmation.snapshot.username} / ${confirmation.snapshot.environment_id}`],["国家",confirmation.snapshot.country],["Final",confirmation.snapshot.video_id],["当前人物",confirmation.snapshot.person_asset_id],["实际商品",`${confirmation.snapshot.selected_product_name} / ${confirmation.snapshot.selected_product_id}`],["文案序号",confirmation.snapshot.description_id]].map(([label,value])=><div key={label}><dt className="text-muted-foreground">{label}</dt><dd className="mt-1 break-all">{value}</dd></div>)}</dl>
   <p className="whitespace-pre-wrap rounded-lg bg-muted p-3 leading-6">{confirmation.caption}</p>
   <details><summary className="cursor-pointer">查看身份哈希与内容审查证据</summary><pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap text-[10px]">{JSON.stringify({snapshot:confirmation.snapshot,content_review:confirmation.content_review,fingerprint:confirmation.fingerprint},null,2)}</pre></details>
   <label className="flex items-start gap-2 leading-6"><input aria-label="确认本条账号视频商品文案公开发布" type="checkbox" checked={checked} disabled={busy||running||unresolved||unknown||uploadOnly||verified} onChange={e=>setChecked(e.target.checked)}/><span>我已核对上述账号、当前成片、商品及文案，确认仅此一条可以公开发布。</span></label>
   <Button size="sm" disabled={busy||!checked||running||unresolved||unknown||uploadOnly||verified} onClick={submit}>{busy?"正在检查真实提交状态…":"确认并提交本条"}</Button>
  </div>}
  {verified&&row?.platform_url&&<a className="text-xs underline" href={row.platform_url} target="_blank" rel="noreferrer">查看后台已核实的发布视频</a>}
 </section>;
}
