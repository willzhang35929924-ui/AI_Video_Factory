"use client";

import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { request } from "@/lib/api";
import { Panel } from "@/components/factory-ui";
import { Button } from "@/components/ui/button";

export function LocalCutoutImport({ onCreated }: { onCreated: (id: string) => void | Promise<unknown> }) {
  const chooser = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const mutation = useMutation({ mutationFn: async () => {
    if (!file) throw new Error("请先选择本地原始视频");
    const body = new FormData(); body.append("video", file);
    return request<{ task_id: string; run_id: string; message: string }>("/api/local-cutout/import", { method: "POST", body, signal: AbortSignal.timeout(120000) });
  }, onSuccess: async result => { await onCreated(result.task_id); } });
  return <Panel title="导入本地原始视频" description="直接使用已有原片：自动校验、抠像、保存独立 RGB / Alpha、质检；不采集、不下载、不请求背景。">
    <div className="flex flex-wrap items-center gap-3">
      <input ref={chooser} type="file" aria-label="选择本地原片" accept=".mp4,.mov,.mkv,.webm,.m4v,.avi" className="hidden" onChange={event => { setFile(event.target.files?.[0] || null); mutation.reset(); }} />
      <Button variant="outline" disabled={mutation.isPending} onClick={() => chooser.current?.click()}>选择本地视频</Button>
      <span className="text-xs text-muted-foreground">{file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB` : "未选择文件"}</span>
      <Button disabled={!file || mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? "上传原片…" : "导入并开始抠像"}</Button>
    </div>
    <div aria-live="polite" className="mt-3 text-xs">
      {mutation.isError && <p role="alert" className="text-red-600">{mutation.error.message}</p>}
      {mutation.data && <p>{mutation.data.message} · <Link href={`/tasks/${mutation.data.task_id}`} className="underline">查看任务进度</Link></p>}
    </div>
    <p className="mt-3 text-xs leading-5 text-muted-foreground">未关联国家/产品的原片明确记为“未分类 / 未关联产品”，不会根据文件名猜测。人物需人工审核后用于正式合成；登录、队列或模型未就绪时，保留原片并显示等待原因。</p>
  </Panel>;
}
