"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { request } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { StatusBadge } from "@/components/factory-ui";
import { DescriptionReuse } from "@/components/description-reuse";

type GeneratedDescription = {id:string;text:string;weight:number;enabled:boolean};
type ProductReview = {product_id:string;descriptions:GeneratedDescription[];sha256:string;status:string;required:boolean;generation_id?:string|null};
type Generation = {id:string;status:string;model:string;prompt_version:string;error_code?:string;error_message?:string;descriptions:GeneratedDescription[];current_product?:ProductReview;evidence?:{model_called?:boolean;sync_pid?:number;content_review?:{status:string;sha256:string};readback?:{written_cells?:string[];readback_sha256?:string}}};
type Config = {model:string;base_url:string;sync_to_tencent:boolean;readiness:{status:string;missing:string[];message:string}};
const active = ["QUEUED","READING_SOURCE","SUBMITTED","SYNCING"];

export function DescriptionGenerator({productId,onReady}:{productId:string;onReady:(descriptions:GeneratedDescription[])=>void}) {
  const client=useQueryClient(),[open,setOpen]=useState(false),[reviewOpen,setReviewOpen]=useState(false),[retry,setRetry]=useState(false),[confirmed,setConfirmed]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState("");
  const config=useQuery({queryKey:["publishing","description-config"],queryFn:()=>request<Config>("/api/publishing/descriptions/config")});
  const history=useQuery({queryKey:["publishing","description-generations",productId],queryFn:()=>request<Generation[]>(`/api/publishing/products/${productId}/descriptions/generations`),refetchInterval:q=>q.state.data?.some(x=>active.includes(x.status))?1500:false});
  const current=useQuery({queryKey:["publishing","description-content-review",productId],queryFn:()=>request<ProductReview>(`/api/publishing/products/${productId}/descriptions/content-review`)});
  const [reviewSnapshot,setReviewSnapshot]=useState<ProductReview|null>(null);
  const latest=history.data?.[0],running=latest&&active.includes(latest.status);
  const loaded=useRef("");
  useEffect(()=>{if(latest?.status==="READY"&&latest.current_product&&loaded.current!==latest.id){loaded.current=latest.id;void client.invalidateQueries({queryKey:["publishing","products"]});void current.refetch();onReady(latest.current_product.descriptions.map(({id,text,weight,enabled})=>({id,text,weight,enabled})));}},[client,current,latest,onReady]);
  async function run(sync=false){setBusy(true);setError("");try{await request(sync?`/api/publishing/descriptions/${latest?.id}/sync`:`/api/publishing/products/${productId}/descriptions/generate`,{method:"POST",body:sync?undefined:JSON.stringify({confirm_external_request:confirmed,retry_generation_id:retry?latest?.id:undefined})});await client.invalidateQueries({queryKey:["publishing"]});if(!sync)setOpen(false);}catch(e){setError(e instanceof Error?e.message:"生成未确认");}finally{setBusy(false);}}
  async function openReview(){setBusy(true);setError("");setConfirmed(false);try{const result=await current.refetch();if(result.error)throw result.error;if(!result.data)throw new Error("当前已保存文案未加载");setReviewSnapshot(result.data);setReviewOpen(true);}catch(e){setError(e instanceof Error?e.message:"文案未加载");}finally{setBusy(false);}}
  async function approveContent(){if(!reviewSnapshot)return;setBusy(true);setError("");try{await request(`/api/publishing/products/${productId}/descriptions/content-review`,{method:"POST",body:JSON.stringify({confirm_facts:confirmed,sha256:reviewSnapshot.sha256})});await Promise.all([history.refetch(),current.refetch()]);await client.invalidateQueries({queryKey:["publishing","products"]});setReviewOpen(false);}catch(e){setError(e instanceof Error?e.message:"核验未保存");}finally{setBusy(false);}}
  return <div className="mt-4 space-y-3 border-t pt-4">
    <DescriptionReuse productId={productId} onReady={onReady}/>
    <div className="flex flex-wrap items-center gap-2"><Button variant="outline" size="sm" disabled={busy||!!running} onClick={()=>{setRetry(false);setConfirmed(false);setOpen(true);void config.refetch();}}>AI 生成20条文案</Button><Button variant="ghost" size="sm" onClick={()=>{void history.refetch();void current.refetch();void config.refetch();}}>刷新文案状态</Button>{latest&&<StatusBadge status={latest.status}/>}</div>
    {config.data&&<p className="text-xs text-muted-foreground">模型：{config.data.model||"未配置"} · {config.data.readiness.status} · 参数统一在 config.yaml，密钥在 .env</p>}
    {latest&&<div role="status" className="text-xs leading-6"><p className="break-all">{latest.id} · {latest.model} · {latest.descriptions.length}/20</p><p>{latest.error_code} {latest.error_message}</p>{latest.evidence?.readback?.readback_sha256&&<p>云文档读回已验证 · 写入 {latest.evidence.readback.written_cells?.length||0} 个空单元格</p>}{latest.status==="WAITING_MANUAL"&&latest.descriptions.length>0&&<Button variant="outline" size="sm" disabled={busy} onClick={()=>run(true)}>只核实/继续写回，不重新调用AI</Button>}</div>}
    {current.data?.required&&current.data.descriptions.length>0&&<p className="text-xs leading-6 text-muted-foreground">生成、导入和云文档读回不代表产品事实已核验。材质、功效、适用场景等必须有依据。当前产品已保存文案：{current.data.descriptions.length} 条 · 内容核验：{current.data.status}。{current.data.status!=="APPROVED"&&<Button variant="outline" size="sm" className="ml-2" disabled={busy} onClick={openReview}>核验已保存文案</Button>}</p>}
    {(error||config.error||history.error||current.error)&&<p role="alert" className="text-xs text-red-600">{error||config.error?.message||history.error?.message||current.error?.message}</p>}
    {latest?.error_code==="DESCRIPTION_INPUT_OR_RESPONSE_INVALID"&&<Button variant="outline" size="sm" disabled={busy} onClick={()=>{setRetry(true);setConfirmed(false);setOpen(true);void config.refetch();}}>明确重试一次文案 API（可能额外计费）</Button>}
    <Dialog open={open} onOpenChange={setOpen}><DialogContent><DialogHeader><DialogTitle>生成产品文案</DialogTitle><DialogDescription>发送当前产品、国家、keyword 与已确认事实到你配置的模型，可能计费。保留已有文案/公式，仅填实际表头对应的空单元格，写后读回验证。未知请求不自动重交。</DialogDescription></DialogHeader>
      <p className="break-all text-xs">{config.data?.base_url||"接口未配置"} · {config.data?.model||"模型未配置"}</p>
      {retry&&<p className="text-xs">将建立新请求，保留失败历史；这不是仅写回重试，可能再次计费。</p>}
      {config.data?.readiness.status!=="READY"&&<p role="alert" className="text-xs">{config.data?.readiness.message} · 缺少：{config.data?.readiness.missing.join("、")}</p>}
      <label className="flex items-center gap-2 text-xs"><input aria-label="确认调用文案模型" type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>确认发送资料并调用一次模型</label>
      <DialogFooter><Button variant="outline" onClick={()=>setOpen(false)}>返回</Button><Button disabled={busy||!confirmed||config.data?.readiness.status!=="READY"} onClick={()=>run()}>生成并写回腾讯文档</Button></DialogFooter>
    </DialogContent></Dialog>
    <Dialog open={reviewOpen} onOpenChange={setReviewOpen}><DialogContent className="max-h-[85vh] overflow-y-auto"><DialogHeader><DialogTitle>核验产品文案事实</DialogTitle><DialogDescription>请先保存任何文案编辑，再核验以下当前产品已保存文本，不是旧生成历史。不合实物的表述请返回编辑。此操作不调用模型、不发布视频；文本更改后需要重新核验。</DialogDescription></DialogHeader><p className="break-all text-[10px] text-muted-foreground">当前产品：{reviewSnapshot?.product_id} · 文本 SHA256：{reviewSnapshot?.sha256}</p>{reviewSnapshot?.descriptions.map(row=><p key={row.id} className="text-xs leading-5">{row.id} · {row.text}</p>)}<label className="flex items-center gap-2 text-xs"><input aria-label="确认文案事实有依据" type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>已核实所有产品事实有依据，无虚构功效、材质或承诺</label><DialogFooter><Button variant="outline" onClick={()=>setReviewOpen(false)}>返回编辑</Button><Button disabled={busy||!confirmed||!reviewSnapshot?.descriptions.length} onClick={approveContent}>确认已保存文案</Button></DialogFooter></DialogContent></Dialog>
  </div>;
}
