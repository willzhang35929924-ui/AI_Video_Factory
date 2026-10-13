"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { EmptyState, FactoryGuard, PageHeading, TaskPicker } from "@/components/factory-ui";
import { Button } from "@/components/ui/button";
import { ManualCompose } from "@/components/manual-compose";
import { RealWorkbench } from "@/components/real-workbench";
import { useFactory } from "@/lib/api";

function Workbench() {
  const factory = useFactory();
  const query = useSearchParams();
  const router = useRouter();
  const [selectedId, setSelectedId] = useState("");
  const [mode, setMode] = useState<"manual" | "automatic">();
  const [pickerOpen, setPickerOpen] = useState(false);
  const tasks = factory.data?.tasks.filter(task => task.execution_mode === "integration") || [];
  const requested = selectedId || query.get("task");
  const task = tasks.find(item => item.id === requested) || tasks.find(item => item.current_stage === "compose") || tasks[0];
  const manualTask = task?.context.production_strategy === "manual_compose";
  const manual = mode ? mode === "manual" : query.get("mode") ? query.get("mode") === "manual" : manualTask;
  function saveView(taskId: string, nextMode?: "manual" | "automatic") {
    const params = new URLSearchParams({ task: taskId });
    if (nextMode) params.set("mode", nextMode);
    router.replace("/compose?" + params.toString(), { scroll: false });
  }
  return <>
    <PageHeading eyebrow="COMPOSITION WORKBENCH" title="合成工作台" description="自动复用同一 RGB + Alpha 母资产，按编号合成并保留版本和真实执行记录。">
      {task && <><TaskPicker tasks={tasks} value={task.id} onChange={id => { setSelectedId(id); setMode(undefined); setPickerOpen(false); saveView(id); }} />
        <Button variant={manual ? "outline" : "default"} title="查看原自动生产工作台；切换视图不会开始生产" onClick={() => { setMode("automatic"); saveView(task.id, "automatic"); }}>自动合成</Button>
        <Button variant={manual ? "default" : "outline"} onClick={() => { setMode("manual"); setPickerOpen(true); saveView(task.id, "manual"); }}>手动合成</Button></>}
    </PageHeading>
    <FactoryGuard factory={factory}>
      {task ? manual ? <ManualCompose task={task} assets={factory.data?.assets || []} reviews={factory.data?.reviews || []} runs={factory.data?.runs || []} onChanged={factory.refresh} pickerOpen={pickerOpen} onPickerOpenChange={setPickerOpen}/> : <RealWorkbench key={task.id} kind="compose" task={task} factory={factory}/> : <EmptyState title="还没有可合成的真实任务" description="先创建并绑定产品的真实任务，再选择已有 RGB / Alpha 或上传透明人物与背景。这里不会创建示例任务或假素材。"><Link href="/collector" className="text-xs underline underline-offset-4">查看任务创建入口</Link></EmptyState>}
    </FactoryGuard>
  </>;
}

export default function Page() {
  return <Suspense fallback={<p className="text-xs text-muted-foreground">正在加载工作台…</p>}><Workbench/></Suspense>;
}
