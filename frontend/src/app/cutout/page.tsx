"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { EmptyState, FactoryGuard, PageHeading, TaskPicker } from "@/components/factory-ui";
import { RealWorkbench } from "@/components/real-workbench";
import { ProductionReadinessPanel } from "@/components/production-ui";
import { useFactory } from "@/lib/api";
import { LocalCutoutImport } from "@/components/local-cutout-import";

function Workbench() {
  const factory = useFactory();
  const query = useSearchParams();
  const [selectedId, setSelectedId] = useState("");
  const tasks = factory.data?.tasks.filter(task => task.execution_mode === "integration") || [];
  const requested = selectedId || query.get("task");
  const task = tasks.find(item => item.id === requested) || tasks.find(item => item.current_stage === "cutout") || tasks.find(item => factory.data?.assets.some(asset => asset.task_id === item.id && asset.type === "source" && asset.registry_status === "registered")) || tasks[0];
  return <>
    <PageHeading eyebrow="PORTRAIT WORKBENCH" title="抠像工作台" description="检查人物边缘与时序稳定性，定位需要重新处理的区间。">
      {task && <TaskPicker tasks={tasks} value={task.id} onChange={setSelectedId} />}
    </PageHeading>
    <FactoryGuard factory={factory}>
      <div className="mb-6"><LocalCutoutImport onCreated={async id => { await factory.refresh(); setSelectedId(id); }} /></div>
      {task ? <RealWorkbench key={task.id} kind="cutout" task={task} factory={factory}/> : <EmptyState title="还没有可处理的视频" description="先在采集控制台创建正常生产任务。"><Link href="/collector" className="text-xs underline underline-offset-4">新建采集任务</Link></EmptyState>}
    </FactoryGuard>
    {task && <div className="mt-6"><ProductionReadinessPanel stage="cutout" taskId={task.id}/></div>}
  </>;
}

export default function Page() {
  return <Suspense fallback={<p className="text-xs text-muted-foreground">正在加载工作台…</p>}><Workbench/></Suspense>;
}
