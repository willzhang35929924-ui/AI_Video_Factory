"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { API_URL, request } from "@/lib/api";
import type { Asset } from "@/lib/types";
import type { PersonTransform } from "@/components/transform-controls";
import { Field } from "@/components/factory-ui";
import { Button } from "@/components/ui/button";

type Media = { width: number; height: number; frame_count: number; fps: number };
type FrameImage = { image: ImageBitmap; bounds?: { x: number; y: number; width: number; height: number } };

async function loadImage(url: string, signal: AbortSignal, alphaBounds = false): Promise<FrameImage> {
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`预览媒体读取失败 (${response.status})`);
  const image = await createImageBitmap(await response.blob());
  if (!alphaBounds) return { image };
  const canvas = document.createElement("canvas");
  canvas.width = image.width; canvas.height = image.height;
  const context = canvas.getContext("2d")!;
  context.drawImage(image, 0, 0);
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
  let left = canvas.width, top = canvas.height, right = -1, bottom = -1;
  for (let y = 0; y < canvas.height; y++) for (let x = 0; x < canvas.width; x++) {
    if (pixels[(y * canvas.width + x) * 4 + 3] > 16) {
      left = Math.min(left, x); top = Math.min(top, y); right = Math.max(right, x); bottom = Math.max(bottom, y);
    }
  }
  return { image, bounds: right < 0 ? undefined : { x: left, y: top, width: right - left + 1, height: bottom - top + 1 } };
}

export function PersonPositionPreview({ taskId, person, backgrounds, value, onChange, disabled, width = 1080, height = 1920 }: {
  taskId: string; person: Asset; backgrounds: Asset[]; value: PersonTransform; onChange: (value: PersonTransform) => void;
  disabled?: boolean; width?: number; height?: number;
}) {
  const base = `/api/tasks/${encodeURIComponent(taskId)}/person/${encodeURIComponent(person.id)}`;
  const media = useQuery({ queryKey: ["position-media", taskId, person.id], queryFn: () => request<{ media: Media }>(base + "/info"), retry: false });
  const [frame, setFrame] = useState(0);
  const [decodedFrame, setDecodedFrame] = useState(0);
  useEffect(() => { const timer = window.setTimeout(() => setDecodedFrame(frame), 180); return () => window.clearTimeout(timer); }, [frame]);
  const [backgroundId, setBackgroundId] = useState(backgrounds.at(-1)?.id || "");
  const canvas = useRef<HTMLCanvasElement>(null);
  const drag = useRef<{ x: number; y: number; value: PersonTransform } | null>(null);
  const portrait = useQuery({ queryKey: ["position-frame", taskId, person.id, decodedFrame],
    queryFn: ({ signal }) => loadImage(`${API_URL}${base}/frame?channel=rgba&frame=${decodedFrame}`, signal, true),
    enabled: !!media.data, retry: false, staleTime: 60000, gcTime: 0 });
  const background = useQuery({ queryKey: ["position-background", backgroundId],
    queryFn: ({ signal }) => loadImage(`${API_URL}/api/assets/${encodeURIComponent(backgroundId)}/content`, signal),
    enabled: !!backgroundId, retry: false, staleTime: 60000, gcTime: 60000 });
  const source = media.data?.media;
  const factor = source ? Math.min(width / source.width, height / source.height) * value.scale : 1;
  const fittedWidth = source ? Math.max(1, Math.round(source.width * factor)) : width;
  const fittedHeight = source ? Math.max(1, Math.round(source.height * factor)) : height;
  const left = (width - fittedWidth) / 2 + value.x, top = (height - fittedHeight) / 2 + value.y;
  const bounds = portrait.data?.bounds;
  const box = useMemo(() => bounds && portrait.data ? { x: left + bounds.x / portrait.data.image.width * fittedWidth,
    y: top + bounds.y / portrait.data.image.height * fittedHeight, width: bounds.width / portrait.data.image.width * fittedWidth,
    height: bounds.height / portrait.data.image.height * fittedHeight } : undefined, [bounds, portrait.data, left, top, fittedWidth, fittedHeight]);
  useEffect(() => {
    const context = canvas.current?.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, width, height);
    for (let y = 0; y < height; y += 80) for (let x = 0; x < width; x += 80) {
      context.fillStyle = (x / 80 + y / 80) % 2 ? "#d4d4d4" : "#ededed";
      context.fillRect(x, y, 80, 80);
    }
    if (background.data) {
      const image = background.data.image, fit = Math.max(width / image.width, height / image.height);
      context.drawImage(image, (width - image.width * fit) / 2, (height - image.height * fit) / 2, image.width * fit, image.height * fit);
    }
    if (portrait.data) context.drawImage(portrait.data.image, left, top, fittedWidth, fittedHeight);
    if (box) { context.strokeStyle = "#2563eb"; context.lineWidth = Math.max(2, width / 300); context.setLineDash([12, 8]); context.strokeRect(box.x, box.y, box.width, box.height); }
  }, [background.data, portrait.data, left, top, fittedWidth, fittedHeight, width, height, box]);
  const error = media.error || portrait.error || background.error;
  return <div className="space-y-3">
    <Field label="位置预览背景" hint="读取此任务已有背景；未选择时显示透明棋盘，不生成新背景。">
      <select aria-label="位置预览背景" value={backgroundId} onChange={event => setBackgroundId(event.target.value)} className="h-10 min-w-0 rounded-lg border bg-background px-3 text-xs">
        <option value="">透明棋盘</option>{backgrounds.map(asset => <option key={asset.id} value={asset.id}>{asset.name}</option>)}
      </select>
    </Field>
    <canvas ref={canvas} width={width} height={height} aria-label="人物位置实时预览" data-person-asset-id={person.id} data-frame={frame}
      data-transform={JSON.stringify(value)} className="mx-auto max-h-[420px] max-w-full touch-none rounded-lg border" style={{ aspectRatio: `${width}/${height}`, cursor: disabled ? "default" : "grab" }}
      onPointerDown={event => { if (disabled || !portrait.data) return; drag.current = { x: event.clientX, y: event.clientY, value }; event.currentTarget.setPointerCapture(event.pointerId); }}
      onPointerMove={event => { if (!drag.current) return; const rect = event.currentTarget.getBoundingClientRect(); onChange({ ...drag.current.value,
        x: Math.round(drag.current.value.x + (event.clientX - drag.current.x) * width / rect.width),
        y: Math.round(drag.current.value.y + (event.clientY - drag.current.y) * height / rect.height) }); }}
      onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }} />
    <Field label={`预览帧 ${frame} · ${source ? (frame / source.fps).toFixed(2) : "0.00"} 秒`} hint="拖动画面改变 X/Y；数字输入精确调整。蓝框为此帧 Alpha 可见范围。">
      <input aria-label="位置预览帧" type="range" min={0} max={Math.max(0, (source?.frame_count || 1) - 1)} value={frame}
        disabled={!source || disabled} onChange={event => setFrame(event.target.valueAsNumber)} className="w-full" />
    </Field>
    <div aria-live="polite" className="text-xs text-muted-foreground">
      {(media.isFetching || portrait.isFetching || background.isFetching) && <p>正在读取真实 RGB / Alpha 帧…</p>}
      {error && <div className="space-y-2"><p role="alert" className="text-red-600">{error.message}</p><Button variant="outline" size="sm" disabled={media.isFetching || portrait.isFetching || background.isFetching} onClick={() => { void media.refetch(); if (media.data) void portrait.refetch(); if (backgroundId) void background.refetch(); }}>重新读取预览</Button></div>}
      {box && <p>Bounding box：X {Math.round(box.x)} · Y {Math.round(box.y)} · {Math.round(box.width)} × {Math.round(box.height)}；画布 {width} × {height}</p>}
      <p>这是当前帧的位置预览，不是已生成的视频。Apply 保存后，合成使用相同 X/Y/Scale；上方历史视频保持原版本。</p>
    </div>
  </div>;
}
