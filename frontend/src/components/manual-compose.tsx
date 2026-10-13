"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Film, Plus, Upload } from "lucide-react";
import { Field, Panel, StatusBadge } from "@/components/factory-ui";
import { Button } from "@/components/ui/button";
import { AssetPicker, type ComposeAssetKind } from "@/components/asset-picker";
import { API_URL, request } from "@/lib/api";
import type { Asset, ProcessingRun, Review, SavedBackgroundReuse, Task } from "@/lib/types";

type RenderResponse = { run_id: string; preview: boolean; preview_url: string; asset: Asset | null; review: Review | null;
  manifest: { output_sha256: string; output_media: { width: number; height: number; fps: number; frame_count: number }; cutout_inference_executions: number } };
type SavedManifest = RenderResponse["manifest"] & { mode: "PREVIEW" | "COMPOSITION"; processing_run_id: string; person_asset_id: string; background_asset_id: string };
type RenderJob = { run_id: string; status: "QUEUED" | "RUNNING" | "READY" | "FAILED"; phase?: string; run: ProcessingRun; result?: RenderResponse };
type VersionsResponse = { current_person_asset_id?: string; latest_composition_asset_id?: string; saved_backgrounds_supported?: boolean; saved_backgrounds?: SavedBackgroundReuse; selection?: { person_asset_id?: string; background_asset_id?: string }; job?: { run_id: string; status: string; phase?: string; stage?: string; manifest?: SavedManifest } };
export type ManualComposeProps = { task: Task; assets: Asset[]; reviews?: Review[]; runs?: ProcessingRun[]; onChanged?: () => void | Promise<unknown>;
  pickerOpen: boolean; onPickerOpenChange: (open: boolean) => void };

export function ManualCompose(props: ManualComposeProps) {
  return <ManualComposeEditor key={props.task.id} {...props} />;
}

function ManualComposeEditor({ task, assets, reviews = [], runs = [], onChanged, pickerOpen, onPickerOpenChange }: ManualComposeProps) {
  const client = useQueryClient();
  const path = `/api/manual-compose/tasks/${encodeURIComponent(task.id)}`;
  const configuration = useQuery({ queryKey: ["manual-compose-config"], queryFn: () => request<{ manual_import_max_bytes: number }>("/api/manual-compose/config"), retry: false });
  const uploadLimit = configuration.data?.manual_import_max_bytes;
  const uploadLimitText = uploadLimit ? uploadLimit >= 1024 ** 3 ? `${(uploadLimit / 1024 ** 3).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} GiB（${uploadLimit.toLocaleString()} 字节）` : `${uploadLimit.toLocaleString()} 字节` : undefined;
  const versions = useQuery({ queryKey: ["manual-transforms", task.id], queryFn: () => request<VersionsResponse>(path + "/transforms"), refetchInterval: 2000, retry: false });
  const eligible = assets.filter(asset => asset.registry_status === "registered" && !["rejected", "failed", "media_deleted"].includes(asset.status.toLowerCase())
    && asset.legacy_reference?.execution_scope !== "TECHNICAL_VALIDATION");
  const people = eligible.filter(asset => asset.type === "person" && asset.legacy_reference?.alpha_path);
  const backgrounds = eligible.filter(asset => asset.type === "background");
  const ownBackgrounds = backgrounds.filter(asset => asset.task_id === task.id);
  const [chosenPerson, setChosenPerson] = useState<string>();
  const [chosenBackground, setChosenBackground] = useState<string>();
  const [pickerKind, setPickerKind] = useState<ComposeAssetKind>("person");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [pendingRun, setPendingRun] = useState<{ runId: string; signature: string }>();
  const [rgbFile, setRgbFile] = useState<File>();
  const [alphaFile, setAlphaFile] = useState<File>();
  const [backgroundFile, setBackgroundFile] = useState<File>();
  const rgbInput = useRef<HTMLInputElement>(null);
  const backgroundInput = useRef<HTMLInputElement>(null);
  const production = task.context.production as { current_person_asset_id?: string } | undefined;
  const currentPersonId = versions.data?.current_person_asset_id || production?.current_person_asset_id || "";
  const savedPersonId = versions.data?.selection?.person_asset_id;
  const personId = chosenPerson || savedPersonId || currentPersonId;
  const person = people.find(asset => asset.id === personId);
  const currentPerson = assets.find(asset => asset.id === currentPersonId && asset.task_id === task.id);
  const manualRejected = task.status?.toUpperCase() === "REJECTED" || currentPerson?.status.toUpperCase() === "REJECTED";
  const currentPersonValid = !!person && personId === currentPersonId && !manualRejected && !["REJECTED", "FAILED"].includes(person.status.toUpperCase());
  const rejectedReview = reviews.filter(review => review.asset_id === currentPersonId && review.status.toLowerCase() === "rejected").at(-1)
    || reviews.find(review => review.id === (task.context.production as { rejected_review?: { review_id?: string } } | undefined)?.rejected_review?.review_id);
  const transform = person?.legacy_reference?.transform as { x?: number; y?: number; scale?: number } | undefined;
  const latestComposition = eligible.find(asset => asset.task_id === task.id && asset.id === versions.data?.latest_composition_asset_id && asset.type === "compose");
  const reuse = versions.data?.saved_backgrounds;
  const jobBackgroundId = versions.data?.job?.manifest?.background_asset_id;
  const savedBackgroundId = versions.data?.selection?.background_asset_id;
  const backgroundId = chosenBackground || (backgrounds.some(asset => asset.id === savedBackgroundId) ? savedBackgroundId : undefined)
    || (backgrounds.some(asset => asset.id === jobBackgroundId) ? jobBackgroundId : undefined)
    || String(latestComposition?.legacy_reference?.background_asset_id || "") || ownBackgrounds.at(-1)?.id || "";
  const composition = eligible.filter(asset => asset.task_id === task.id && asset.type === "compose" && asset.legacy_reference?.person_asset_id === personId
    && asset.legacy_reference?.background_asset_id === backgroundId).at(-1) || latestComposition;
  const signature = JSON.stringify([personId, backgroundId]);
  const activeRunId = pendingRun?.runId || (versions.data?.job?.stage === "compose" && ["QUEUED", "RUNNING"].includes(versions.data.job.status) ? versions.data.job.run_id : undefined);
  const job = useQuery({ queryKey: ["manual-compose-run", activeRunId], enabled: !!activeRunId,
    queryFn: () => request<RenderJob>(`/api/manual-compose/runs/${encodeURIComponent(activeRunId!)}`), retry: false,
    refetchInterval: query => ["READY", "FAILED"].includes(query.state.data?.status || "") ? false : 1500 });
  const completedRun = job.data?.status === "READY" ? job.data.run_id : undefined;
  useEffect(() => { if (completedRun) { void client.invalidateQueries({ queryKey: ["factory"] }); void client.invalidateQueries({ queryKey: ["manual-transforms", task.id] }); } }, [client, completedRun, task.id]);
  const jobManifest = versions.data?.job?.status === "READY" ? versions.data.job.manifest : undefined;
  const compositionRun = runs.find(run => run.id === composition?.legacy_reference?.processing_run_id && run.provider === "FactoryManualCompose" && run.status === "success");
  const compositionManifest = compositionRun?.resource_info?.manual_compose as SavedManifest | undefined;
  const savedManifest = jobManifest?.mode === "PREVIEW" ? jobManifest : compositionManifest?.mode === "COMPOSITION"
    && compositionManifest.processing_run_id === compositionRun?.id ? compositionManifest : jobManifest?.mode === "COMPOSITION" ? jobManifest : undefined;
  const savedSignature = savedManifest && JSON.stringify([savedManifest.person_asset_id, savedManifest.background_asset_id]);
  const storedResult: RenderResponse | undefined = savedManifest && savedSignature === signature ? { run_id: savedManifest.processing_run_id,
    preview: savedManifest.mode === "PREVIEW", preview_url: `/api/manual-compose/runs/${encodeURIComponent(savedManifest.processing_run_id)}/preview`,
    manifest: savedManifest, asset: savedManifest.mode === "COMPOSITION" ? composition || null : null,
    review: savedManifest.mode === "COMPOSITION" ? reviews.find(review => review.asset_id === composition?.id && review.status === "pending") || null : null } : undefined;
  const rendered = pendingRun?.signature === signature ? job.data?.result || storedResult : storedResult;
  const compositionReview = reviews.filter(review => review.asset_id === rendered?.asset?.id).at(-1);
  const running = busy !== "" || ["QUEUED", "RUNNING"].includes(job.data?.status || versions.data?.job?.status || "") || runs.some(run => run.task_id === task.id && ["running", "queued"].includes(run.status));
  const selectedReview = reviews.filter(review => review.asset_id === personId && review.status === "pending").at(-1);
  const run = runs.find(run => run.id === rendered?.run_id || run.id === versions.data?.job?.run_id);
  const previewUrl = rendered ? API_URL + rendered.preview_url : person ? `${API_URL}/api/assets/${encodeURIComponent(person.id)}/preview?channel=black` : undefined;

  async function action(label: string, work: () => Promise<{ message?: string } | void>) {
    setBusy(label); setError(""); setMessage("");
    try { const response = await work(); setMessage(response?.message || label + "完成"); await versions.refetch(); await onChanged?.(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "操作失败，请检查本地运行日志"); }
    finally { setBusy(""); }
  }
  async function render(preview: boolean) {
    const response = await request<{ run_id: string; message: string }>("/api/manual-compose/render", { method: "POST", body: JSON.stringify({ task_id: task.id, person_asset_id: personId, background_asset_id: backgroundId, preview }) });
    setPendingRun({ runId: response.run_id, signature }); return response;
  }
  async function prepareSavedReuse(label: string) {
    if (running || !currentPersonValid || !composition) return;
    await action(label, () => request<{ message?: string }>(path + "/saved-backgrounds/prepare", { method: "POST", signal: AbortSignal.timeout(120000) }));
  }
  async function selectAsset(asset: Asset) {
    setBusy("保存素材选择"); setError(""); setMessage("");
    try {
      const response = await request<{ message: string }>(path + "/selection", { method: "PUT", body: JSON.stringify({ kind: pickerKind, asset_id: asset.id }), signal: AbortSignal.timeout(120000) });
      if (pickerKind === "person") setChosenPerson(asset.id); else setChosenBackground(asset.id);
      setPendingRun(undefined); setMessage(response.message);
      await versions.refetch(); await onChanged?.();
    } catch (failure) { setError(failure instanceof Error ? failure.message : "素材选择未能保存"); throw failure; }
    finally { setBusy(""); }
  }
  async function upload(kind: "person" | "background") {
    const form = new FormData();
    if (kind === "person" && rgbFile) { form.append("rgb_file", rgbFile); if (alphaFile) form.append("alpha_file", alphaFile); }
    if (kind === "background" && backgroundFile) form.append("image", backgroundFile);
    const response = await request<{ asset: Asset; message: string }>(path + "/upload-" + kind, { method: "POST", body: form, signal: AbortSignal.timeout(3600000) });
    await request(path + "/selection", { method: "PUT", body: JSON.stringify({ kind, asset_id: response.asset.id }), signal: AbortSignal.timeout(120000) });
    if (kind === "person") setChosenPerson(response.asset.id); else setChosenBackground(response.asset.id);
    setPendingRun(undefined); return response;
  }
  return <div className="space-y-5">
    <Panel title="手动合成" description="选择已有 RGB / Alpha 人物和背景，或上传真实素材。只运行本地合成，不采集、不抠像、不请求付费背景。" action={<StatusBadge status={running ? "RUNNING" : rendered ? "READY" : "WAITING_MANUAL"} />}>
      <div className="grid gap-7 lg:grid-cols-[minmax(240px,.9fr)_minmax(280px,1fr)]">
        <div className="min-w-0">
          <div className="relative flex min-h-80 items-center justify-center overflow-hidden rounded-lg bg-neutral-950">
            {previewUrl ? <video key={previewUrl} src={previewUrl} controls playsInline preload="metadata" aria-label={rendered ? "真实合成视频" : "人物母版黑底预览"} className="max-h-[560px] w-full object-contain" onError={() => setError("视频未能加载，请检查文件校验与本地媒体接口")} />
              : <div className="px-6 py-20 text-center text-neutral-400"><Film className="mx-auto mb-4" size={28} /><p className="text-sm">上传透明人物，或选择已登记版本</p><p className="mt-2 text-xs">普通 RGB 视频必须配套 Alpha 遮罩</p></div>}
          </div>
          <p className="mt-3 text-xs text-muted-foreground">{rendered ? rendered.preview ? "短片预览 · 非最终资产" : compositionReview?.status === "approved" ? "完整合成 · 当前版本已通过人工审核" : compositionReview?.status === "rejected" ? "完整合成 · 当前版本审核未通过" : "完整合成 · 待人工审核" : "人物母版 · 黑底 · 尚未合成背景"}</p>
          {rendered && <div className="mt-4 space-y-2 border-t pt-4 font-mono text-[11px] tabular-nums text-muted-foreground">
            <p>{rendered.manifest.output_media.width} × {rendered.manifest.output_media.height} · {rendered.manifest.output_media.fps} fps · {rendered.manifest.output_media.frame_count} 帧</p>
            <p className="break-all">SHA256 {rendered.manifest.output_sha256}</p><p className="break-all">Run {rendered.run_id}{run?.pid ? ` · PID ${run.pid}` : ""} · 抠像执行 0 次</p>
          </div>}
        </div>
        <div className="space-y-5">
          <div className="space-y-3">
            {([["person", "添加素材库视频", person], ["background", "添加素材库背景", backgrounds.find(asset => asset.id === backgroundId)]] as const).map(([kind, label, selected]) => <div key={kind} className="rounded-lg border p-4">
              <div className="flex flex-wrap items-center justify-between gap-3"><p className="text-xs font-medium">{kind === "person" ? "人物 RGB + Alpha" : "背景图片"}</p><Button variant="outline" size="sm" disabled={running} onClick={() => { setPickerKind(kind); onPickerOpenChange(true); }}><Plus size={14}/>{label}</Button></div>
              <p className="mt-3 break-all text-xs">{selected?.name || "尚未选择真实素材"}</p>{selected && <><p className="mt-2 text-[11px] text-muted-foreground">{selected.width} × {selected.height} · {selected.duration} s · {selected.status}</p><p className="mt-2 break-all text-[10px] text-muted-foreground">{selected.source_system || "UNKNOWN"} · {selected.id}</p></>}
            </div>)}
          </div>
          <p className="text-xs leading-5 text-muted-foreground">使用当前人物版本已保存的位置。位置编辑在 Review 完成；此处选择素材不会恢复版本或批准审核。</p>
          {person && <p className="text-xs text-muted-foreground">Review 已保存位置：X {transform?.x ?? 0} · Y {transform?.y ?? 0} · 缩放 {transform?.scale ?? 1}；此处只读。</p>}
          {(!person || !backgroundId) && <p className="text-xs text-muted-foreground">{!person ? "先选择当前有效 RGB / Alpha 人物，或上传配对素材。" : "先选择或上传真实背景图片。"}准备完成后才能开始合成。</p>}
          <div className="flex flex-wrap gap-2 border-t pt-5">
            <Button variant="outline" disabled={running || !person || !backgroundId} onClick={() => void action("生成短片预览", () => render(true))}>生成短片预览</Button>
            <Button disabled={running || !person || !backgroundId} onClick={() => void action("生成完整合成", () => render(false))}>生成完整合成</Button>
            {selectedReview && <Link href={`/review?task=${encodeURIComponent(task.id)}&review=${encodeURIComponent(selectedReview.id)}`} className="self-center text-xs underline underline-offset-4">打开人物审核</Link>}
            {rendered?.review && <Link href={`/review?task=${encodeURIComponent(task.id)}&review=${encodeURIComponent(rendered.review.id)}`} className="self-center text-xs underline underline-offset-4">打开质量审核</Link>}
          </div>
          {person && person.task_id !== task.id && <p className="break-all text-xs text-muted-foreground">复用来源任务 {person.task_id} 的人物母资产；不会改变来源任务的人物版本或审核记录。</p>}
        </div>
      </div>
      <div aria-live="polite" className="mt-5 text-xs">{running && <><p className="text-muted-foreground">{busy || "后台本地合成与校验"}中{job.data?.phase || versions.data?.job?.phase ? ` · ${job.data?.phase || versions.data?.job?.phase}` : ""}；可离开页面，返回后读取已保存 Run。</p>{activeRunId && <p className="mt-2 break-all font-mono">Run {activeRunId} · PID {job.data?.run.pid || "待启动"}</p>}</>}{message && <p>{message}</p>}{error && <p role="alert" className="break-words text-red-600">{error}</p>}{job.data?.status === "FAILED" && <p role="alert" className="text-red-600">本地 Run 失败：{job.data.run.error_message || job.data.run.error_code}</p>}{job.isError && <p role="alert" className="text-red-600">Run 读取失败：{job.error.message}</p>}{versions.isError && <p role="alert" className="text-red-600">手动合成状态读取失败：{versions.error.message}</p>}</div>
    </Panel>
    {(reuse || versions.data?.saved_backgrounds_supported && composition && ownBackgrounds.length >= 20) && <Panel title="同源人物 · 已有背景自动复用" description="仅使用已登记的真实背景，保留当前 X / Y / Scale。不重新采集、抠像或生成付费图片。首条成片必须由你审核；通过后后台串行处理其余背景。" action={reuse ? <StatusBadge status={manualRejected ? "WAITING_MANUAL" : reuse.status}/> : undefined}>
      {manualRejected && <div role="alert" className="mb-4 text-xs leading-6 text-amber-700"><p>当前{currentPerson?.status.toUpperCase() === "REJECTED" ? "人物版本" : "任务"}已被人工拒绝：{rejectedReview?.note || "请在审核中心处理当前版本，不能用旧授权继续生产。"} 已有母资产、背景和续跑记录{reuse ? `（${reuse.status}）` : ""}保留；不会重试或覆盖拒绝。</p><Link className="underline underline-offset-4" href={`/review?task=${encodeURIComponent(task.id)}${rejectedReview ? `&review=${encodeURIComponent(rejectedReview.id)}` : ""}`}>查看人物审核与拒绝原因</Link></div>}
      {reuse ? <>
        <p className="mb-4 break-all text-xs text-muted-foreground">人物 {reuse.person_asset_id || "UNKNOWN"} · {reuse.items.filter(item => item.status === "READY" || item.status === "COMPLETED").length} / {reuse.items.length} 项已有真实结果{reuse.batch_id ? ` · Batch ${reuse.batch_id}` : ""}</p>
        {reuse.status === "WAITING_INPUTS" && <p role="alert" className="mb-4 text-xs text-amber-700">{reuse.message || reuse.reason || "已有背景来源关联未满足，未启动合成。"}</p>}
        <p className="mb-4 text-xs text-muted-foreground">Alpha 几何质检：{reuse.quality?.status || "UNKNOWN"} · 语义完整性：{reuse.quality?.semantic_status || "UNKNOWN"}。人工批准与机器质检分开保存，几何提示不会改成 PASS。</p>
        {!manualRejected && reuse.status === "WAITING_FIRST_REVIEW" && reuse.first_review_id && <Link className="text-xs underline underline-offset-4" href={`/review?task=${encodeURIComponent(task.id)}&review=${encodeURIComponent(reuse.first_review_id)}`}>审核首条成片：通过后自动继续，拒绝则停止扩展</Link>}
        {reuse.error && <p role="alert" className="mt-3 break-all text-xs text-red-600">{reuse.error}</p>}
        <div className="mt-4 overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr className="border-b text-muted-foreground"><th className="py-3">编号</th><th>状态</th><th>执行 / 资产</th></tr></thead><tbody>{reuse.items.map(item => <tr key={item.step_key || item.background_asset_id} className="border-b"><td className="py-3 tabular-nums">{String(item.ordinal).padStart(2, "0")}</td><td><StatusBadge status={item.status}/>{item.error && <p className="mt-1 max-w-sm break-all text-red-600">{item.error}</p>}{item.status === "FAILED" && <Button variant="outline" size="sm" disabled={running || !currentPersonValid} className="mt-2" onClick={() => { if (!running && currentPersonValid) void action(`重试背景 ${item.ordinal}`, () => request<{ message?: string }>(path + "/saved-backgrounds/retry", { method: "POST", body: JSON.stringify({ ordinal: item.ordinal }) })); }}>仅重试 {String(item.ordinal).padStart(2, "0")}</Button>}</td><td className="max-w-md py-3"><p className="break-all font-mono text-[10px]">背景 {item.background_asset_id}</p><p className="mt-1 break-all font-mono text-[10px]">{item.run_id || "尚未执行"}</p>{item.asset_id && <Link href={`/assets/${encodeURIComponent(item.asset_id)}`} className="mt-1 block break-all underline underline-offset-4">查看真实成片 {item.asset_id}</Link>}{item.output_sha256 && <p className="mt-1 break-all font-mono text-[10px] text-muted-foreground">SHA {item.output_sha256}</p>}</td></tr>)}</tbody></table></div>
        <div className="mt-4 border-t pt-4"><p className="mb-3 text-xs text-muted-foreground">恢复中断的旧检查点时，重新核验当前人物、首条成片与已有背景。不代替人工批准，不重做首条、抠像或付费背景；已执行的编号保留。</p><Button variant="outline" disabled={running || !currentPersonValid || !composition} onClick={() => void prepareSavedReuse("重新核验已有背景复用")}>重新核验已有背景复用</Button></div>
      </> : <><p className="mb-3 text-xs text-muted-foreground">此成片来自旧续跑检查点。核验当前母资产、完整成片与已有背景后，未审核则等待首条审核；已亲自批准则自动进入原队列，复用其余背景。不批准审核、不重新生成首条或付费图片。</p><Button variant="outline" disabled={running || !currentPersonValid || !composition} onClick={() => void prepareSavedReuse("核验已有背景复用")}>核验已有背景复用</Button></>}
    </Panel>}
    <Panel title="上传本地素材" description={`原始上传保留。人物仅接受含真实 Alpha 的视频，或 RGB 视频与单通道 Alpha 视频配对；${uploadLimitText ? `每个文件最多 ${uploadLimitText}。` : "正在读取上传限额。"}`}>
      <div className="grid gap-6 lg:grid-cols-2">
        <div className="space-y-3"><Field label="人物 RGB / 透明视频"><input ref={rgbInput} aria-label="人物 RGB 或透明视频文件" type="file" accept=".mkv,.mov,.mp4,.webm" disabled={running} onChange={event => setRgbFile(event.target.files?.[0])} className="w-full text-xs" /></Field><Field label="独立 Alpha 遮罩（单文件含 Alpha 时可留空）"><input aria-label="人物 Alpha 遮罩文件" type="file" accept=".mkv,.mov,.mp4,.webm" disabled={running} onChange={event => setAlphaFile(event.target.files?.[0])} className="w-full text-xs" /></Field><Button variant="outline" disabled={running || !uploadLimit} onClick={() => rgbFile ? void action("上传人物", () => upload("person")) : rgbInput.current?.click()}><Upload size={14} />上传并验证人物</Button>{!rgbFile && <p className="text-xs text-muted-foreground">先打开文件选择器，选择后再点击上传；普通 MP4 还需要独立 Alpha。</p>}{error.includes("PERSON_ALPHA_REQUIRED") && <Link href={`/cutout?task=${encodeURIComponent(task.id)}`} className="text-xs underline underline-offset-4">这是原始 RGB 视频：进入抠像生成独立 RGB / Alpha</Link>}</div>
        <div className="space-y-3"><Field label="背景图片"><input ref={backgroundInput} aria-label="背景图片文件" type="file" accept=".png,.jpg,.jpeg,.webp" disabled={running} onChange={event => setBackgroundFile(event.target.files?.[0])} className="w-full text-xs" /></Field><p className="text-xs leading-5 text-muted-foreground">背景按画布铺满裁切；复用人物当前已登记版本，不改变母资产。</p><Button variant="outline" disabled={running || !uploadLimit} onClick={() => backgroundFile ? void action("上传背景", () => upload("background")) : backgroundInput.current?.click()}><Upload size={14} />上传并验证背景</Button>{!backgroundFile && <p className="text-xs text-muted-foreground">先选择图片，选择后再点击上传并验证。</p>}</div>
      </div>
      {configuration.isError && <div className="mt-3 flex flex-wrap items-center gap-3"><p role="alert" className="text-xs text-red-600">上传限额读取失败：{configuration.error.message}</p><Button variant="outline" size="sm" disabled={configuration.isFetching} onClick={() => void configuration.refetch()}>重新检查上传限额</Button></div>}
    </Panel>
    <AssetPicker key={`${pickerKind}:${pickerOpen}`} open={pickerOpen} onOpenChange={onPickerOpenChange} kind={pickerKind} taskId={task.id} currentPersonId={currentPersonId} assets={assets} reviews={reviews} selectedId={pickerKind === "person" ? personId : backgroundId} onSelect={selectAsset}/>
  </div>;
}
