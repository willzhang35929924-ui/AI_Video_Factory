"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { EmptyState, FactoryGuard, PageHeading, TaskPicker } from "@/components/factory-ui";
import { RealWorkbench } from "@/components/real-workbench";
import { useFactory } from "@/lib/api";

function Workbench() {
  const factory = useFactory();
  const query = useSearchParams();
  const [selectedId, setSelectedId] = useState("");
  const tasks = factory.data?.tasks.filter(task => task.execution_mode === "integration") || [];
  const requested = selectedId || query.get("task");
  const usableAssets = factory.data?.assets.filter(asset => asset.registry_status === "registered" && !["failed", "rejected", "FAILED", "REJECTED"].includes(asset.status) && asset.legacy_reference?.execution_scope !== "TECHNICAL_VALIDATION") || [];
  const task = tasks.find(item => item.id === requested)
    || tasks.find(item => item.status !== "REJECTED" && usableAssets.some(asset => asset.task_id === item.id && asset.type === "person" && asset.id === (item.context.production as Record<string, unknown> | undefined)?.current_person_asset_id))
    || tasks.find(item => item.status !== "REJECTED" && usableAssets.some(asset => asset.task_id === item.id && asset.type === "source"))
    || tasks[0];
  return <>
    <PageHeading eyebrow="BACKGROUND WORKBENCH" title="背景工作台" description="生产线自动生成提示词与背景；先审核首条成片，再自动扩展 02–20。">
      {task && <TaskPicker tasks={tasks} value={task.id} onChange={setSelectedId} />}
    </PageHeading>
    <FactoryGuard factory={factory}>
      {task ? <RealWorkbench key={task.id} kind="background" task={task} factory={factory}/> : <EmptyState title="还没有可生成背景的任务" description="先在采集控制台创建正常生产任务。"><Link href="/collector" className="text-xs underline underline-offset-4">新建采集任务</Link></EmptyState>}
    </FactoryGuard>
  </>;
}

export default function Page() {
  return <Suspense fallback={<p className="text-xs text-muted-foreground">正在加载工作台…</p>}><Workbench/></Suspense>;
}
