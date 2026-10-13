"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { usePathname } from "next/navigation";
import { useNotice } from "@/components/providers";
import { taskSchema, type ActionResponse, type Asset, type FactoryState, type Readiness, type ProductionQueue } from "./types";
export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "/factory-api").replace(/\/$/, "");
export function productionSocketUrl(apiBase: string, path: string, pageUrl: string) {
  const url = new URL(apiBase.replace(/\/$/, "") + path, pageUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}
export function isManualReviewAsset(asset?:Asset) {
  const reference=asset?.legacy_reference;
  return !!asset && ["person","compose","final"].includes(asset.type) && reference?.execution_scope!=="TECHNICAL_VALIDATION"
    && (["FactoryTransform","FactoryManualUpload","FactoryManualCompose"].includes(asset.source_system || "") || reference?.manual_compose===true || reference?.manual_upload===true || !!reference?.transform_version_id);
}
export function isMaskEditablePerson(asset?:Asset) {
  const reference=asset?.legacy_reference;
  return !!asset && asset.type==="person" && asset.registry_status==="registered"
    && reference?.alpha_mode==="straight" && !!reference.alpha_path && !!reference.manifest_path
    && reference.full_decode_verified===true;
}
export class ApiRequestError extends Error {
  constructor(public status:number,public detail:unknown) {
    const data=detail&&typeof detail==="object"?detail as {message?:string;code?:string;checks?:Readiness["checks"]}:null;
    const failures=(data?.checks || []).filter(check=>check.required!==false&&check.status!=="READY"&&check.ok!==true);
    const reason=failures.map(check=>`${check.name}：${check.message || check.error || check.error_code || check.status}`).join("；");
    super([typeof detail==="string"?detail:data?.message || data?.code || "输入不符合接口要求",reason].filter(Boolean).join(" · "));
    this.name="ApiRequestError";
  }
}
export async function request<T>(path:string, options?:RequestInit):Promise<T> {
  const response=await fetch(API_URL+path,{...options,headers:{...(options?.body instanceof FormData?{}:{"Content-Type":"application/json"}),...options?.headers},signal:options?.signal || AbortSignal.timeout(10000)});
  if(!response.ok){const body=await response.json().catch(()=>({detail:"请求失败"}));throw new ApiRequestError(response.status,body.detail);}
  return response.json() as Promise<T>;
}
export async function submitFactoryAction(input:{action:string;entity_id?:string;task_id?:string;payload?:unknown}):Promise<ActionResponse> {
  try {
    return await request<ActionResponse>("/api/actions",{method:"POST",body:JSON.stringify(input),
      ...(["production.resume","production.resume_cutout"].includes(input.action)?{signal:AbortSignal.timeout(120000)}:{})});
  } catch(error) {
    if(input.action!=="production.resume" || error instanceof ApiRequestError)throw error;
    const payload=input.payload && typeof input.payload==="object"?input.payload as {batch_id?:string}:undefined;
    if(payload?.batch_id) {
      try {
        const queue=await request<ProductionQueue>("/api/production/queue",{signal:AbortSignal.timeout(120000)});
        if(queue.batch_id===payload.batch_id && ["QUEUED","RUNNING","COMPLETED","COMPLETED_WITH_ERRORS"].includes(queue.status)) {
          return {accepted:true,status:queue.status,message:`响应未及时返回，已只读核实原队列 ${queue.status}；未重复提交恢复请求。`};
        }
      } catch { /* Keep the submitted request unconfirmed; never retry the POST. */ }
    }
    return {accepted:false,status:"UNKNOWN",message:"恢复请求响应未确认；可能仍在核验。请查看原队列和运行记录，不要重复点击恢复；系统未重发请求。"};
  }
}
export async function recomposePerson(taskId:string,person:Asset,background?:Asset) {
  const technical=person.legacy_reference?.execution_scope==="TECHNICAL_VALIDATION";
  const manual=isManualReviewAsset(person);
  if(manual && (!background || background.task_id!==taskId || background.type!=="background"
    || background.registry_status!=="registered" || background.legacy_reference?.execution_scope==="TECHNICAL_VALIDATION")) {
    throw new Error("缺少此任务已登记的真实背景；请先选择或上传背景，不会生成替代素材。");
  }
  return request<{run_id?:string;status?:string;message?:string}>(technical?`/api/tasks/${taskId}/technical-validation/compose`:manual?"/api/manual-compose/render":"/api/actions",{
    method:"POST",signal:AbortSignal.timeout(30000),body:JSON.stringify(technical?{}:manual?
      {task_id:taskId,person_asset_id:person.id,background_asset_id:background!.id}
      :{action:"production.retry",entity_id:taskId,payload:{stage:"compose"}}),
  });
}
export function useFactory(scopeOverride?: "production" | "history" | "technical") {
  const client=useQueryClient();const notify=useNotice();
  const pathname=usePathname();
  const scope=scopeOverride || (pathname==="/publish" || pathname.startsWith("/publish/") ? "raw" : "production");
  const query=useQuery({queryKey:["factory",scope],queryFn:async()=>{const data=await request<FactoryState>("/api/state?scope="+scope);return {...data,tasks:data.tasks.map(task=>taskSchema.parse(task))};},refetchInterval:30000});
  const mutation=useMutation({mutationFn:(input:{action:string;entity_id?:string;task_id?:string;payload?:unknown;manualReview?:boolean})=>input.manualReview
    ? request<ActionResponse>(`/api/manual-compose/reviews/${encodeURIComponent(input.entity_id || "")}/${input.action==="review.reject"?"reject":"approve"}`,{method:"POST",body:JSON.stringify({note:(input.payload as {note?:string}|undefined)?.note || ""}),signal:AbortSignal.timeout(120000)})
    : submitFactoryAction(input),onSuccess:async(result)=>{notify(result.message || "操作已提交");await client.invalidateQueries({queryKey:["factory"]});await client.invalidateQueries({queryKey:["production-queue"]});},onError:(error)=>notify(error.message)});
  async function act(action:string,entity_id?:string,payload:unknown={}) {
    const linkedTaskId = query.data?.tasks.find(task=>task.id===entity_id)?.id
      || query.data?.assets.find(asset=>asset.id===entity_id)?.task_id
      || query.data?.reviews.find(review=>review.id===entity_id)?.task_id
      || query.data?.prompts.find(prompt=>prompt.id===entity_id)?.task_id
      || query.data?.collector.find(row=>row.id===entity_id)?.last_task_id
      || query.data?.publishing.find(item=>item.id===entity_id)?.task_id;
    const realTask=query.data?.tasks.find(task=>task.id===linkedTaskId&&task.execution_mode==="integration");
    const review=query.data?.reviews.find(item=>item.id===entity_id);
    const manualReview=!!realTask&&isManualReviewAsset(query.data?.assets.find(asset=>asset.id===review?.asset_id));
    const production=realTask?.context.production as {current_person_asset_id?:string}|undefined;
    const manualPerson=!!realTask&&isManualReviewAsset(query.data?.assets.find(asset=>asset.id===production?.current_person_asset_id));
    if(manualReview&&action.startsWith("review.")&&!["review.approve","review.reject"].includes(action)){
      notify("手动任务请调整位置版本或重新上传素材；当前没有此手动审核操作，不会调用旧重试或重新采集。");
      return null;
    }
    if(manualPerson&&action.startsWith("production.")&&!["production.pause","production.cancel","production.skip"].includes(action)){
      notify("手动任务请使用手动合成工作台；不会进入自动采集、抠像或付费背景流程。");
      return null;
    }
    if(realTask&&!action.startsWith("production.")&&!action.startsWith("review.")&&!["start_collection","retry_collection","resume_collection","cancel_collection"].includes(action)){
      notify("请使用生产流程操作此真实任务；未连接的 Mock 操作不能更新真实结果。");
      return null;
    }
    try{return await mutation.mutateAsync({action,entity_id,payload,...(manualReview&&["review.approve","review.reject"].includes(action)?{manualReview:true}:{})});}catch{return null;}
  }
  return {...query,act,working:mutation.isPending,refresh:()=>query.refetch()};
}
export function useCollectorRules() {
  return useQuery({queryKey:["collector-rules"],queryFn:()=>request<Record<string,unknown>>("/api/integrations/collector/rules"),retry:false});
}
export function useAppMode() {
  return useQuery({queryKey:["health"],queryFn:()=>request<{app_mode:string;publishing_backend?:string;publishing_production_readiness?:string}>("/api/health"),refetchInterval:10000});
}
export function useProductionReadiness(stage?:string,taskId?:string) {
  const client=useQueryClient();const notify=useNotice();
  const params=new URLSearchParams();if(stage)params.set("stage",stage);if(taskId)params.set("task_id",taskId);
  const path="/api/production/readiness?"+params.toString();const key=["production-readiness",stage || "all",taskId || ""];
  const query=useQuery({queryKey:key,queryFn:()=>request<Readiness>(path,{signal:AbortSignal.timeout(30000)}),retry:false,refetchInterval:30000});
  const mutation=useMutation({mutationFn:()=>request<Readiness>(path+"&refresh=true",{signal:AbortSignal.timeout(30000)}),onSuccess:data=>client.setQueryData(key,data),onError:error=>notify(error.message)});
  return {...query,isFetching:query.isFetching || mutation.isPending,check:()=>mutation.mutate()};
}
export function useProductionQueue() {return useQuery({queryKey:["production-queue"],queryFn:()=>request<ProductionQueue>("/api/production/queue"),retry:false,refetchInterval:30000});}
export async function collectionDryRun(taskId:string) {
  return request<Record<string,unknown>>("/api/integrations/collector/dry-run?task_id="+encodeURIComponent(taskId));
}
export function useProductionEvents(taskId?:string){
  const client=useQueryClient();
  useEffect(() => {
    const url = productionSocketUrl(API_URL, taskId ? "/ws/tasks/" + encodeURIComponent(taskId) : "/ws/production", window.location.href);
    let socket: WebSocket | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;
    let delay = 500;
    function connect() {
      if (disposed) return;
      const active = new WebSocket(url);
      socket = active;
      active.onopen = () => { if (disposed) active.close(); else delay = 500; };
      active.onmessage = message => {
        if (disposed) return;
        try { if (JSON.parse(message.data).type === "heartbeat") return; } catch { return; }
        if (refreshTimer !== undefined) return;
        refreshTimer = setTimeout(() => {
          refreshTimer = undefined;
          if (disposed) return;
          void client.invalidateQueries({ queryKey: ["factory"] });
          void client.invalidateQueries({ queryKey: ["production-queue"] });
        }, 2000);
      };
      active.onclose = () => {
        if (disposed) return;
        timer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, 5000);
      };
    }
    timer = setTimeout(connect, 0);
    return () => {
      disposed = true;
      clearTimeout(timer);
      clearTimeout(refreshTimer);
      if (socket?.readyState === WebSocket.OPEN) socket.close();
    };
  }, [client, taskId]);
}
export function downloadJson(name:string,data:unknown) {const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));a.download=name;a.click();URL.revokeObjectURL(a.href);}
