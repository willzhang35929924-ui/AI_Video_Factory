"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { StatusBadge } from "@/components/factory-ui";
import { API_URL, request } from "@/lib/api";
import { type Asset, type Review, durationLabel, timeLabel } from "@/lib/types";

export type ComposeAssetKind = "person" | "background";
export type AssetMediaStatus = { asset_id: string; available: boolean; preview_available: boolean; status: string };
type AssetLibrary = { current_person_asset_ids: Record<string, string | null>; blocked_task_ids: string[] };
const types = [["all", "全部类型"], ["person", "RGB + Alpha 人物"], ["background", "背景图片"],
  ["source", "原始视频"], ["source_candidate", "候选链接"], ["compose", "合成视频"], ["final", "最终视频"], ["exception", "异常"]];

export function composeAssetReason(asset: Asset, kind: ComposeAssetKind, taskId: string, currentPersonId: string, media?: AssetMediaStatus, library?: AssetLibrary) {
  if (asset.legacy_reference?.execution_scope === "TECHNICAL_VALIDATION") return "技术验证素材不能进入生产合成";
  if (asset.registry_status !== "registered") return "尚未登记为真实生产素材";
  if (["rejected", "failed"].includes(asset.status.toLowerCase())) return "该素材已拒绝或失败";
  if (asset.type !== kind) return kind === "person" && asset.type === "final"
    ? "最终成片只有 RGB，不能冒充 RGB + Alpha 人物" : kind === "person" ? "需要已登记的 RGB + Alpha 人物" : "需要背景图片";
  if (kind === "background" && asset.codec && !["png", "mjpeg", "webp"].includes(asset.codec)) return "背景需要单帧 PNG、JPEG 或 WebP 图片；此素材不是可用图片";
  if (kind === "person") {
    const reference = asset.legacy_reference;
    if (!reference?.alpha_path || !reference.alpha_sha256 || !reference.manifest_path || reference.alpha_mode !== "straight")
      return "缺少已登记的 RGB / Alpha 配对证据";
    const ownerCurrentId = asset.task_id === taskId ? currentPersonId : library?.current_person_asset_ids[asset.task_id];
    if (asset.task_id !== taskId && !library) return "正在核对来源任务当前人物版本";
    if (library?.blocked_task_ids.includes(asset.task_id)) return "来源任务已被人工拒绝；请先在 Review 修复";
    if (asset.id !== ownerCurrentId) return "不是来源任务当前人物版本；请先在 Review 显式恢复";
  }
  if (!media) return "正在核对实际媒体文件";
  if (!media.available || !media.preview_available) return `媒体不可用：${media.status}`;
  return "";
}

export function filterPickerAssets(assets: Asset[], type: string, status: string, search: string) {
  const keyword = search.trim().toLowerCase();
  return assets.filter(asset => (type === "all" || asset.type === type) && (status === "all" || asset.status === status)
    && `${asset.id} ${asset.name} ${asset.country} ${asset.keyword} ${asset.task_id} ${asset.source_system || ""} ${asset.source_url || ""}`.toLowerCase().includes(keyword));
}

export function AssetPicker({ open, onOpenChange, kind, taskId, currentPersonId, assets, reviews = [], selectedId, onSelect }: {
  open: boolean; onOpenChange: (open: boolean) => void; kind: ComposeAssetKind; taskId: string; currentPersonId: string;
  assets: Asset[]; reviews?: Review[]; selectedId?: string; onSelect: (asset: Asset) => void | Promise<void>;
}) {
  const client = useQueryClient();
  const [type, setType] = useState<string>(kind);
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [failedMedia, setFailedMedia] = useState<string[]>([]);
  const [selectingId, setSelectingId] = useState("");
  const [selectionError, setSelectionError] = useState("");
  const [pendingId, setPendingId] = useState(selectedId || "");
  const media = useQuery({ queryKey: ["asset-media-status", "production", "compose-library"],
    queryFn: () => request<AssetMediaStatus[]>("/api/assets/media-status?scope=production"), enabled: open, retry: false, refetchInterval: open ? 5000 : false });
  const library = useQuery({ queryKey: ["manual-compose-library"], queryFn: () => request<AssetLibrary>("/api/manual-compose/library"),
    enabled: open && kind === "person", retry: false, refetchInterval: open && kind === "person" ? 5000 : false });
  const visible = filterPickerAssets(assets, type, status, search).sort((left, right) =>
    Number(right.id === selectedId || right.id === currentPersonId) - Number(left.id === selectedId || left.id === currentPersonId)
    || Number(right.task_id === taskId) - Number(left.task_id === taskId)
    || right.created_at.localeCompare(left.created_at));
  const statuses = [...new Set(assets.map(asset => asset.status))].sort();
  async function refresh() { setFailedMedia([]); await Promise.all([media.refetch(), library.refetch(), client.invalidateQueries({ queryKey: ["factory"] })]); }
  async function choose(asset: Asset) {
    setSelectingId(asset.id); setSelectionError("");
    try { await onSelect(asset); onOpenChange(false); }
    catch (failure) { setSelectionError(failure instanceof Error ? failure.message : "素材选择未能保存，请重新核对"); }
    finally { setSelectingId(""); }
  }
  const pending = assets.find(asset => asset.id === pendingId);
  const pendingReason = pending && composeAssetReason(pending, kind, taskId, currentPersonId, media.data?.find(row => row.asset_id === pending.id), library.data);
  return <Dialog open={open} onOpenChange={value => { if (!selectingId) onOpenChange(value); }}><DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-5xl">
    <DialogHeader><DialogTitle>{kind === "person" ? "添加视频 · 选择 RGB + Alpha 人物" : "添加背景 · 选择真实图片"}</DialogTitle>
      <DialogDescription>可复用其他任务的真实背景及当前有效 RGB / Alpha 人物，保留来源关联。原片、合成视频和 Final 不能代替 Alpha。选择后点击确认；取消不会修改已保存配置。</DialogDescription></DialogHeader>
    <div className="flex flex-wrap gap-3">
      <div className="relative min-w-52 flex-1"><Search className="absolute left-3 top-3 text-muted-foreground" size={14}/>
        <Input aria-label="搜索合成素材" placeholder="搜索名称、素材 ID、来源、国家或任务" value={search} onChange={event => setSearch(event.target.value)} className="pl-9"/></div>
      <select aria-label="合成素材类型" value={type} onChange={event => setType(event.target.value)} className="h-9 rounded-lg border bg-background px-3 text-xs">
        {types.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
      <select aria-label="合成素材状态" value={status} onChange={event => setStatus(event.target.value)} className="h-9 rounded-lg border bg-background px-3 text-xs">
        <option value="all">全部状态</option>{statuses.map(value => <option key={value} value={value}>{value}</option>)}</select>
      <Button variant="outline" size="sm" disabled={media.isFetching} onClick={refresh}><RefreshCw size={14}/>重新核对媒体</Button>
    </div>
    <p className="text-[11px] text-muted-foreground">任务 {taskId} · {visible.length} 项。文件状态只核对存在与大小；开始合成时会重新校验 SHA、Alpha 与时间轴。</p>
    {media.isError && <p role="alert" className="text-xs text-red-600">媒体状态读取失败：{media.error.message}；请重新核对，未启用素材选择。</p>}
    {kind === "person" && library.isError && <p role="alert" className="text-xs text-red-600">来源人物版本读取失败：{library.error.message}；请重新核对。</p>}
    {selectionError && <p role="alert" className="text-xs text-red-600">{selectionError}；选择器保留，未开始合成。</p>}
    {!visible.length ? <p className="rounded-lg border p-6 text-xs text-muted-foreground">没有匹配的素材。调整类型、状态或搜索条件；也可关闭后上传真实透明人物与背景。</p>
      : <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">{visible.map(asset => {
        const checked = media.data?.find(row => row.asset_id === asset.id);
        const reason = media.isError ? "媒体状态尚未核实" : kind === "person" && library.isError ? "来源人物版本尚未核实" : failedMedia.includes(asset.id) ? "实际媒体预览失败，请重新核对" : composeAssetReason(asset, kind, taskId, currentPersonId, checked, library.data);
        const available = checked?.available && checked.preview_available && !failedMedia.includes(asset.id);
        const url = `${API_URL}/api/assets/${encodeURIComponent(asset.id)}/${asset.type === "person" ? "preview?channel=black" : "content"}`;
        return <div key={asset.id} className="min-w-0 rounded-xl border bg-card p-3">
          <div className="overflow-hidden rounded-lg bg-muted">{available ? asset.type === "background"
            ? <picture><img src={url} alt={asset.name} loading="lazy" className="aspect-video w-full object-contain" onError={() => setFailedMedia(current => [...current, asset.id])}/></picture>
            : <video aria-label={`${asset.name} 实际素材预览`} src={url} controls playsInline preload="none" className="aspect-video w-full object-contain" onError={() => setFailedMedia(current => [...current, asset.id])}/>
            : <div className="flex aspect-video items-center justify-center p-5 text-center text-xs leading-6 text-muted-foreground">{media.isError ? "媒体状态读取失败" : checked?.status || "正在核对实际媒体…"}</div>}</div>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2"><p className="min-w-0 truncate text-xs font-medium">{asset.name}</p><StatusBadge status={asset.status}/></div>
          <p className="mt-2 text-[11px] text-muted-foreground">{types.find(([value]) => value === asset.type)?.[1]} · {asset.width && asset.height ? `${asset.width} × ${asset.height}` : "尺寸未知"} · {asset.fps ? `${asset.fps} fps · ` : ""}{durationLabel(asset.duration)}</p>
          <p className="mt-2 text-[11px] text-muted-foreground">审核：{reviews.filter(review => review.asset_id === asset.id).at(-1)?.status || "尚无审核记录"}；可合成不等于审核已通过。</p>
          <p className="mt-2 break-all text-[10px] text-muted-foreground">来源 {asset.source_system || "UNKNOWN"} · {asset.country} · {asset.keyword}</p>
          <p className="mt-2 break-all text-[10px] text-muted-foreground">创建 {timeLabel(asset.created_at)} · 任务 {asset.task_id}</p>
          <p className="mt-2 break-all font-mono text-[10px] text-muted-foreground">{asset.id}</p>
          {asset.source_url && <a href={asset.source_url} target="_blank" rel="noreferrer" className="mt-2 inline-block text-[10px] underline underline-offset-4">查看原始来源</a>}
          <p className="mt-3 min-h-10 text-[11px] leading-5 text-muted-foreground">{reason || "可选择；完整校验将在本地 Run 中执行"}</p>
          <Button size="sm" aria-pressed={pendingId === asset.id} variant={pendingId === asset.id ? "default" : "outline"} disabled={!!reason || !!selectingId} onClick={() => { setPendingId(asset.id); setSelectionError(""); }}>{pendingId === asset.id ? "已选中素材" : "选择素材"}</Button>
        </div>;
      })}</div>}
    <div className="flex flex-wrap items-center gap-3 border-t pt-4"><p className="min-w-0 flex-1 break-all text-xs text-muted-foreground">{pending ? `待确认：${pending.name} · ${pending.id}` : "请选择一项真实素材"}</p>
      <Button variant="outline" disabled={!!selectingId} onClick={() => onOpenChange(false)}>取消选择</Button>
      <Button disabled={!pending || !!pendingReason || media.isError || kind === "person" && library.isError || !!selectingId || failedMedia.includes(pendingId)} onClick={() => { if (pending) void choose(pending); }}>{selectingId ? "正在保存选择…" : "确认选择"}</Button></div>
  </DialogContent></Dialog>;
}
