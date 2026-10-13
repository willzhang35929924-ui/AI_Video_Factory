"use client";

import { use } from "react";
import Link from "next/link";
import { ArrowLeft, ArrowUpRight, Check, Clock3, Download, FileJson, Pause, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState, FactoryGuard, PageHeading, Panel, ProgressBar, StatusBadge, Thumbnail } from "@/components/factory-ui";
import { downloadJson, request, useFactory, useProductionEvents } from "@/lib/api";
import { useNotice } from "@/components/providers";
import { durationLabel, stageLabels, stageNames, timeLabel } from "@/lib/types";
import { IntegrationTaskDetail } from "@/components/integration-task-detail";

const stageRoutes = {
  collector: "/collector", analysis: "/collector", cutout: "/cutout", person: "/assets",
  background: "/background", compose: "/compose", review: "/review", publish: "/publish", download: "/assets", final: "/assets", prompt: "/background", quality: "/review",
};
const assetLabels = { source: "原始视频", source_candidate: "候选链接", person: "纯人像", background: "背景", compose: "合成视频", final: "最终视频", exception: "异常素材" };

export default function TaskDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const factory = useFactory();
  const notice = useNotice();
  useProductionEvents(id);
  const data = factory.data;
  const task = data?.tasks.find((item) => item.id === id);
  const product = data?.products.find((item) => item.id === task?.product_id);
  const stages = stageNames.map((name) => data?.stages.find((stage) => stage.task_id === id && stage.name === name));
  const assets = data?.assets.filter((asset) => asset.task_id === id) || [];
  const source = assets.find((asset) => asset.type === "source");
  const runs = data?.runs.filter((run) => run.task_id === id) || [];
  const errors = data?.errors.filter((error) => error.task_id === id) || [];
  const audits = data?.audits.filter((audit) => audit.task_id === id) || [];
  const basePath = task ? `storage/projects/${task.project_id}/tasks/${task.id}` : "";
  async function exportArtifact(kind: "metadata" | "workflow") {
    try {
      const artifact = await request<unknown>(`/api/tasks/${encodeURIComponent(id)}/${kind}`);
      downloadJson(`${id}-${kind}.mock.json`, artifact);
    } catch (error) {
      notice(error instanceof Error ? error.message : "示例数据导出失败");
    }
  }

  if (task?.execution_mode === "integration") return <FactoryGuard factory={factory}><IntegrationTaskDetail task={task} factory={factory} /></FactoryGuard>;

  return (
    <FactoryGuard factory={factory}>
      {!task ? (
        <EmptyState title="任务不存在" description="该任务可能已经被重置。请返回总览选择其他生产任务。">
          <Link href="/dashboard" className="text-xs underline underline-offset-4">返回生产总览</Link>
        </EmptyState>
      ) : (
        <>
          <Link href="/dashboard" className="mb-6 inline-flex items-center gap-2 text-xs text-muted-foreground"><ArrowLeft size={13} />返回生产队列</Link>
          <PageHeading eyebrow={task.id} title={task.keyword} description={`${task.country} · ${product?.name || "未关联产品"}`}>
            <Button variant="outline" disabled={factory.working || task.status !== "running"} title={task.status === "running" ? "暂停当前 Mock 处理" : "只有进行中的任务可以暂停"} onClick={() => factory.act("pause_task", task.id)}><Pause size={14} />暂停任务</Button>
            <Button disabled={factory.working || task.status === "running" || task.status === "completed"} title={task.status === "running" ? "请先暂停当前处理" : task.status === "completed" ? "该任务已经完成" : "重新排队当前步骤"} onClick={() => factory.act("retry_task", task.id)}><RefreshCw size={14} />重试当前步骤</Button>
          </PageHeading>

          <div className="mb-8 flex flex-wrap items-center justify-between gap-6 rounded-xl border px-6 py-5">
            <dl className="grid flex-1 grid-cols-2 gap-x-8 gap-y-5 text-xs sm:grid-cols-4">
              <div><dt className="text-[10px] text-muted-foreground">Task ID</dt><dd className="mt-2 font-mono">{task.id}</dd></div>
              <div><dt className="text-[10px] text-muted-foreground">国家</dt><dd className="mt-2">{task.country}</dd></div>
              <div><dt className="text-[10px] text-muted-foreground">关联产品</dt><dd className="mt-2">{product?.name || "未关联"}</dd></div>
              <div><dt className="text-[10px] text-muted-foreground">当前阶段</dt><dd className="mt-2">{stageLabels[task.current_stage]}</dd></div>
            </dl>
            <div className="w-full border-t pt-4 sm:w-48 sm:border-l sm:border-t-0 sm:pl-6 sm:pt-0"><div className="mb-3 flex items-center justify-between gap-3"><StatusBadge status={task.status} /><span className="text-xs">{task.progress}%</span></div><ProgressBar value={task.progress} /></div>
          </div>

          <Panel title="生产流水线" description="每一步都保留状态与执行记录。" action={<span className="text-[10px] text-muted-foreground">更新于 {timeLabel(task.updated_at)}</span>}>
            <ol aria-label="视频生产阶段" className="mb-8 grid grid-cols-2 gap-4 sm:grid-cols-4 xl:grid-cols-8">
              {stageNames.map((name, index) => {
                const stage = stages[index];
                const active = task.current_stage === name;
                return <li key={name} className={`rounded-lg border px-3 py-4 ${active ? "border-foreground bg-muted/40" : ""}`}><div className="mb-4 flex items-center justify-between"><span className={`flex size-6 items-center justify-center rounded-full text-[10px] ${stage?.status === "success" ? "bg-foreground text-background" : "border text-muted-foreground"}`}>{stage?.status === "success" ? <Check size={12} /> : index + 1}</span>{active && <span className="text-[9px] text-muted-foreground">当前</span>}</div><p className="mb-3 text-xs font-medium">{stageLabels[name]}</p><StatusBadge status={stage?.status || "pending"} /></li>;
              })}
            </ol>
            <div className="overflow-x-auto">
              <table className="w-full whitespace-nowrap text-left text-xs">
                <thead className="border-b text-[10px] text-muted-foreground"><tr>{["阶段", "状态", "开始时间", "结束时间", "耗时", "重试", "错误"].map((label) => <th key={label} className="px-3 py-3 font-normal first:pl-0">{label}</th>)}</tr></thead>
                <tbody>{stageNames.map((name, index) => {
                  const stage = stages[index];
                  return <tr key={name} className="border-b last:border-0"><td className="py-4 pr-3 font-medium">{stageLabels[name]}</td><td className="px-3"><StatusBadge status={stage?.status || "pending"} /></td><td className="px-3 text-[10px]">{timeLabel(stage?.start_time || null)}</td><td className="px-3 text-[10px]">{timeLabel(stage?.end_time || null)}</td><td className="px-3 tabular-nums">{durationLabel(stage?.duration || 0)}</td><td className="px-3">{stage?.retry_count || 0}</td><td className="max-w-64 whitespace-normal px-3 text-[10px] text-red-600 dark:text-red-400">{stage?.error || "—"}</td></tr>;
                })}</tbody>
              </table>
            </div>
            <div className="mt-5 flex flex-wrap justify-between gap-3 border-t pt-4 text-[10px] text-muted-foreground"><span>创建于 {timeLabel(task.created_at)} · 示例步骤不执行真实视频处理</span><Link href={`${stageRoutes[task.current_stage]}?task=${encodeURIComponent(task.id)}`} className="inline-flex items-center gap-1 text-foreground">打开{stageLabels[task.current_stage]}工作台<ArrowUpRight size={13} /></Link></div>
          </Panel>

          <div className="mt-7 grid gap-7 xl:grid-cols-[minmax(0,1fr)_340px]">
            <div className="min-w-0 space-y-7">
              <Panel title="任务资产" description={`此任务关联 ${assets.length} 项示例资产`}>
                {assets.length ? <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">{assets.map((asset) => <Link key={asset.id} href={`/assets/${asset.id}`} className="rounded-lg border p-3 transition-colors hover:bg-muted/40"><Thumbnail src={asset.thumbnail} alt={asset.name} className="mb-3 aspect-video" /><div className="mb-2 flex items-center justify-between gap-2"><span className="text-[10px] text-muted-foreground">{assetLabels[asset.type]}</span><ArrowUpRight size={13} /></div><p className="truncate text-xs font-medium">{asset.name}</p><p className="mt-2 text-[10px] text-muted-foreground">{asset.width} × {asset.height} · {asset.fps} FPS · {durationLabel(asset.duration)}</p><div className="mt-3"><StatusBadge status={asset.status} /></div></Link>)}</div> : <EmptyState title="此任务还没有素材" description="开始采集后，原始视频和后续资产会关联在这里。" />}
              </Panel>
              <Panel title="处理日志" description="显示 Mock 处理的进度和步骤，不执行真实 Worker。" action={<Button variant="ghost" size="sm" onClick={() => downloadJson(`${id}-runs.json`, runs)}><Download size={13} />导出日志</Button>}>
                {runs.length ? <div className="space-y-5">{[...runs].reverse().map((run) => <section key={run.id} className="rounded-lg border"><div className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3"><span className="text-xs font-medium">{stageLabels[run.stage]}<span className="ml-3 font-mono text-[9px] font-normal text-muted-foreground">{run.id}</span></span><StatusBadge status={run.status} /></div><div className="p-4"><div className="mb-4 flex items-center gap-3"><div className="flex-1"><ProgressBar value={run.progress} /></div><span className="text-[10px] tabular-nums">{run.progress}%</span></div><pre className="whitespace-pre-wrap break-all font-mono text-[10px] leading-6 text-muted-foreground">{run.logs.join("\n")}</pre></div></section>)}</div> : <EmptyState title="暂无处理日志" description="启动任务后，这里会按步骤显示模拟日志和进度。" />}
              </Panel>
              <Panel title="操作历史" action={<Clock3 size={15} className="text-muted-foreground" />}>
                {audits.length ? <ol className="space-y-5">{[...audits].reverse().map((audit) => <li key={audit.id} className="flex gap-4"><span className="mt-1 size-1.5 shrink-0 rounded-full bg-muted-foreground" /><div><p className="text-xs leading-6">{audit.message}</p><p className="mt-1 font-mono text-[10px] text-muted-foreground">{timeLabel(audit.created_at)} · {audit.action}</p></div></li>)}</ol> : <p className="text-xs text-muted-foreground">此任务暂无操作记录。后续每次处理和配置变更都会写入历史。</p>}
              </Panel>
            </div>
            <aside className="space-y-7">
              <Panel title="元数据与工作流" action={<FileJson size={16} className="text-muted-foreground" />}>
                <dl className="space-y-5 text-xs"><div><dt className="text-[10px] text-muted-foreground">原始视频关联</dt><dd className="mt-2 font-mono text-[11px]">{task.source_video_id || "尚未关联"}</dd></div><div><dt className="text-[10px] text-muted-foreground">原始规格</dt><dd className="mt-2">{source ? `${source.width} × ${source.height} · ${source.fps} FPS` : "等待采集"}</dd></div><div><dt className="text-[10px] text-muted-foreground">元数据逻辑路径</dt><dd className="mt-2 break-all font-mono text-[10px] leading-5">{basePath}/metadata/metadata.json</dd></div><div><dt className="text-[10px] text-muted-foreground">工作流逻辑路径</dt><dd className="mt-2 break-all font-mono text-[10px] leading-5">{basePath}/workflow/workflow.json</dd></div></dl>
                <p className="my-5 border-t pt-4 text-[10px] leading-5 text-muted-foreground">当前为示例路径和追溯结构。尚未读取实际 ComfyUI 工作流，也未创建媒体文件。</p>
                <div className="grid gap-2"><Button variant="outline" onClick={() => exportArtifact("metadata")}><Download size={14} />导出元数据示例</Button><Button variant="outline" onClick={() => exportArtifact("workflow")}><Download size={14} />导出工作流追溯示例</Button></div>
              </Panel>
              <Panel title="错误记录" description={`${errors.length} 条与此任务关联的错误`}>
                {errors.length ? <div className="space-y-5">{[...errors].reverse().map((error) => <div key={error.id}><p className="text-xs font-medium text-red-600 dark:text-red-400">{error.code}</p><p className="mt-2 text-xs leading-6">{error.message}</p><p className="mt-2 text-[10px] text-muted-foreground">{error.stage} · {timeLabel(error.created_at)}</p></div>)}</div> : <p className="text-xs leading-6 text-muted-foreground">暂未记录错误。处理失败时会保留原因、阶段与发生时间。</p>}
              </Panel>
            </aside>
          </div>
        </>
      )}
    </FactoryGuard>
  );
}
