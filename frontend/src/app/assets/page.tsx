"use client";
import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageHeading, Panel, StatusBadge, EmptyState } from "@/components/factory-ui";
import { API_URL, request } from "@/lib/api";
import { type Asset, durationLabel } from "@/lib/types";
import { AssetDeletionDialog, AssetFamilyDeletionPreview, assetFamilyPlan, type AssetDeletionStatus, type DeleteResult, deletionLabel, fileSize } from "@/components/asset-deletion";

const categories=[ ["all","全部"],["source","原始视频"],["source_candidate","候选链接"],["person","纯人像"],["background","背景"],["compose","合成视频"],["final","最终视频"],["exception","异常"] ];

export default function AssetsPage() {
  const [scope,setScope]=useState("production");
  const [tab,setTab]=useState("all");
  const [search,setSearch]=useState("");
  const [selected,setSelected]=useState<string[]>([]);
  const [deleteResults,setDeleteResults]=useState<DeleteResult[]>([]);
  const records=useQuery({queryKey:["asset-registry",scope],queryFn:()=>request<Asset[]>(`/api/assets?scope=${scope}`),refetchInterval:30000});
  const media=useQuery({queryKey:["asset-media-status",scope],queryFn:()=>request<{asset_id:string;available:boolean;preview_available:boolean;status:string}[]>(`/api/assets/media-status?scope=${scope}`),refetchInterval:30000});
  const usage=useQuery({queryKey:["asset-storage-usage"],queryFn:()=>request<{status:string;logical_bytes:number;allocated_bytes:number;file_count:number;disk_free_bytes:number}>("/api/assets/storage-usage"),refetchOnWindowFocus:false});
  const deletion=useQuery({queryKey:["asset-deletion-status",scope],queryFn:()=>request<AssetDeletionStatus[]>(`/api/assets/deletion-status?scope=${scope}`,{signal:AbortSignal.timeout(30000)}),enabled:false,retry:false,refetchInterval:false,refetchOnWindowFocus:false});
  const rootIds=[...new Set(selected)].sort();
  const family=useQuery({queryKey:["asset-family-deletion-plan",scope,rootIds],queryFn:()=>assetFamilyPlan(rootIds,scope),enabled:rootIds.length>0,retry:false,refetchOnWindowFocus:false});
  const checkedIds=[...new Set([...rootIds,...(family.data?.expanded_asset_ids||[])])];
  const [failedMedia,setFailedMedia]=useState<string[]>([]);
  const assets=(records.data??[]).filter(asset=>asset.status!=="MEDIA_DELETED"&&(scope!=="technical"||asset.legacy_reference?.execution_scope==="TECHNICAL_VALIDATION")&&(tab==="all"||asset.type===tab)&&`${asset.name} ${asset.country} ${asset.keyword} ${asset.task_id}`.toLowerCase().includes(search.toLowerCase()));
  function refresh(){setFailedMedia([]);void records.refetch();void media.refetch();void deletion.refetch();void usage.refetch();}
  return <>
    <PageHeading eyebrow="ASSET LIBRARY / 02" title="素材库" description="保留原始素材与每一步产物。所有资产都可以回到同一个生产任务。"><select aria-label="素材视图" value={scope} onChange={event=>{setScope(event.target.value);setSelected([]);setDeleteResults([]);}} className="h-9 rounded-lg border bg-background px-3 text-xs"><option value="production">生产素材</option><option value="history">历史记录（含 Mock）</option><option value="technical">技术验证</option></select><Button variant="outline" disabled={records.isFetching||media.isFetching||deletion.isFetching} onClick={()=>{refresh();if(rootIds.length)void family.refetch();}}><RefreshCw size={14}/>刷新素材</Button></PageHeading>
    <div className="mb-7 flex flex-wrap items-center justify-between gap-4"><div className="flex flex-wrap gap-1" role="tablist" aria-label="素材类型">{categories.map(([key,label])=><button role="tab" aria-selected={tab===key} key={key} onClick={()=>setTab(key)} className={`rounded-lg px-3 py-2 text-xs ${tab===key?"bg-foreground text-background":"text-muted-foreground hover:bg-muted"}`}>{label}</button>)}</div><div className="relative min-w-0 max-w-full"><Search className="absolute left-3 top-3 text-muted-foreground" size={14}/><Input aria-label="搜索素材" className="w-60 max-w-full pl-9" placeholder="搜索国家、关键词、任务…" value={search} onChange={event=>setSearch(event.target.value)}/></div></div>
    <p role="status" className="mb-5 text-xs leading-6 text-muted-foreground">{usage.data?`Factory 存储：${fileSize(usage.data.logical_bytes)} · 实际分配文件块 ${fileSize(usage.data.allocated_bytes)} · ${usage.data.file_count} 个文件 · 磁盘可用 ${fileSize(usage.data.disk_free_bytes)}${usage.data.status==="PARTIAL"?" · 部分文件未能统计":""}`:usage.isError?"磁盘占用读取失败，请刷新重试":"正在统计 Factory 存储…"}</p>
    <div className="mb-5 flex flex-wrap items-center gap-3 text-xs"><label className="flex items-center gap-2"><input aria-label="选择当前素材" type="checkbox" checked={assets.length>0&&assets.every(asset=>checkedIds.includes(asset.id))} onChange={event=>setSelected(current=>event.target.checked?[...new Set([...current,...assets.map(asset=>asset.id)])]:current.filter(id=>!assets.some(asset=>asset.id===id)))}/>选择当前列表</label><span className="text-muted-foreground">手动选择 {rootIds.length} 项 · 联动 {checkedIds.length} 项</span><AssetDeletionDialog assets={(records.data||[]).filter(asset=>rootIds.includes(asset.id))} scope={scope} label="删除选中及关联" onDeleted={results=>{setDeleteResults(results);setSelected([]);refresh();}}/></div>
    {rootIds.length>0&&<div className="mb-6"><AssetFamilyDeletionPreview plan={family.data} loading={family.isFetching} error={family.error?.message}/></div>}
    {deleteResults.length>0&&<div role="status" className="mb-6 rounded-xl border p-4 text-xs leading-6">已删除 {deleteResults.filter(result=>result.record_deleted||["DELETED","DELETED_WITH_CLEANUP_ERROR"].includes(result.status)).reduce((count,result)=>count+(result.deleted_asset_ids?.length??1),0)} 项素材记录，删除文件留存 {fileSize(deleteResults.reduce((total,result)=>total+(result.released_bytes||0),0))}；关联记录按确认计划处理，未关联任务、删除审计、外部文件及发布回执保留。{deleteResults.some(result=>result.status==="PARTIAL")&&<p className="mt-2 text-amber-700 dark:text-amber-400">PARTIAL：本地家族已删除；真实发布回执和必要关联锚点保留，外部公开内容不删除。</p>}{deleteResults.some(result=>!["DELETED","PARTIAL"].includes(result.status))&&<p className="mt-2 text-red-600">{deleteResults.filter(result=>!["DELETED","PARTIAL"].includes(result.status)).map(result=>`${result.asset_id}: ${result.message||result.code||result.status}`).join("；")}</p>}</div>}
    {records.isPending?<div role="status" className="rounded-xl border p-8 text-xs text-muted-foreground">正在读取资产登记…</div>:records.isError||media.isError?<Panel><p role="alert" className="text-xs leading-6 text-red-600 dark:text-red-400">{records.error?.message||media.error?.message}</p><Button variant="outline" className="mt-4" onClick={refresh}>重新连接</Button></Panel>:assets.length?<div className="grid gap-x-6 gap-y-9 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">{assets.map(asset=>{
      const status=media.data?.find(row=>row.asset_id===asset.id);
      const protection=deletion.data?.find(row=>row.asset_id===asset.id);
      const linked=checkedIds.includes(asset.id)&&!rootIds.includes(asset.id);
      const available=status?.available&&status.preview_available&&!failedMedia.includes(asset.id);
      const label=asset.legacy_reference?.execution_scope==="TECHNICAL_VALIDATION"?"TECHNICAL VALIDATION":asset.registry_status==="mock"?"MOCK HISTORY":"REAL";
      const url=`${API_URL}/api/assets/${encodeURIComponent(asset.id)}/${asset.type==="person"?"preview":"content"}`;
      const unavailable=failedMedia.includes(asset.id)?"媒体不可预览 · 点击查看详情":asset.type==="source_candidate"?"候选链接 · 尚无本地视频":media.isPending?"正在核对媒体文件…":asset.registry_status==="mock"?"Mock 历史记录 · 无真实媒体":!status?.available?"媒体不存在或与登记记录不一致":"预览文件不存在";
      return <div key={asset.id} className="group min-w-0 rounded-xl border bg-card p-3 transition-colors hover:border-foreground/30"><div className="mb-2 flex items-center justify-between"><label className="flex items-center gap-2 text-[10px] text-muted-foreground"><input type="checkbox" aria-label={`选择素材 ${asset.id}`} checked={checkedIds.includes(asset.id)} disabled={linked} title={linked?"随已选素材自动联动；取消其来源素材可解除":undefined} onChange={event=>setSelected(current=>event.target.checked?[...new Set([...current,asset.id])]:current.filter(id=>id!==asset.id))}/>{linked?"自动关联":"选择"}</label><span className="text-[10px] text-muted-foreground">{fileSize(asset.size)}</span></div><Link href={`/assets/${asset.id}${scope!=="production"?`?scope=${scope}`:""}`}>
        <div className="relative overflow-hidden rounded-lg bg-muted">{available?asset.type==="background"?<picture><img src={url} alt={asset.name} className="aspect-[4/5] w-full object-cover" onError={()=>setFailedMedia(current=>[...current,asset.id])}/></picture>:<video aria-label={`${asset.name}：实际媒体缩略预览`} className="aspect-[4/5] w-full object-cover" muted playsInline preload="metadata" src={url} onError={()=>setFailedMedia(current=>[...current,asset.id])}/>:<div className="flex aspect-[4/5] items-center justify-center p-6 text-center text-xs leading-6 text-muted-foreground">{unavailable}</div>}<span className="absolute bottom-3 right-3 rounded bg-background/90 px-2 py-1 text-[10px]">{durationLabel(asset.duration)}</span><span className="absolute left-3 top-3 rounded bg-background/90 px-2 py-1 text-[9px]">{label} · {categories.find(([key])=>key===asset.type)?.[1]}</span></div>
        <div className="px-1 pb-1 pt-4"><div className="flex flex-wrap items-center justify-between gap-2"><p className="text-xs font-semibold">{asset.country}</p><StatusBadge status={asset.status}/></div><h2 className="mt-2 truncate text-sm">{asset.keyword}</h2><p className="mt-2 text-[10px] text-muted-foreground">{asset.width&&asset.height?`${asset.width} × ${asset.height}`:"分辨率未知"} · {asset.fps?`${asset.fps} FPS`:"FPS 未知"} · {asset.codec||"编码未知"}</p><p className="mt-3 break-all border-t pt-3 font-mono text-[10px] text-muted-foreground">{asset.task_id}</p></div>
      </Link><div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t pt-3"><span className="max-w-full text-[10px] text-muted-foreground">{protection?deletionLabel(protection.status):deletion.isError?"删除保护信息读取失败":deletion.isFetching?"正在核对删除保护…":"点击查看保护以核对引用"}</span><AssetDeletionDialog assets={[asset]} scope={scope} label="删除及关联" onDeleted={results=>{setDeleteResults(results);refresh();}}/></div></div>;
    })}</div>:<EmptyState title="没有匹配的素材" description="调整类型或关键词，或从采集页面创建生产任务。"/>}
  </>;
}
