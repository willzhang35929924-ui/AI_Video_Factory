"use client";

import { Field } from "@/components/factory-ui";
import { Button } from "@/components/ui/button";
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { request } from "@/lib/api";
import type { Asset, Task } from "@/lib/types";
import { PersonPositionPreview } from "@/components/person-position-preview";

export type PersonTransform = { x: number; y: number; scale: number };
export type TransformVersion = PersonTransform & {
  id: string;
  person_asset_id: string;
  parent_person_asset_id: string;
  created_at: string;
};

export type TransformControlsProps = {
  value: PersonTransform;
  onChange: (value: PersonTransform) => void;
  onApply: () => void;
  onUndo: () => void;
  onRestore: (personAssetId: string) => void;
  versions: TransformVersion[];
  currentPersonAssetId?: string;
  disabled?: boolean;
  changed?: boolean;
};

export function TransformControls({ value, onChange, onApply, onUndo, onRestore, versions, currentPersonAssetId, disabled, changed }: TransformControlsProps) {
  const selectedVersion = versions.find(version => version.person_asset_id === currentPersonAssetId);
  function update(key: keyof PersonTransform, number: number) {
    if (Number.isFinite(number) && (key !== "scale" || number > 0)) onChange({ ...value, [key]: number });
  }
  return <div className="space-y-6">
    <div className="grid grid-cols-3 gap-3">
      {(["x", "y", "scale"] as const).map(key => <Field key={key} label={key === "scale" ? "Scale · 倍率" : `${key.toUpperCase()} · 像素`}>
        <input aria-label={key === "scale" ? "人物缩放倍率" : `人物 ${key.toUpperCase()} 坐标`} type="number" step={key === "scale" ? .05 : 1} min={key === "scale" ? .01 : undefined} value={value[key]} disabled={disabled}
          onChange={event => update(key, event.target.valueAsNumber)} className="h-10 min-w-0 rounded-lg border bg-background px-3 font-mono text-sm tabular-nums focus-visible:outline-2 focus-visible:outline-offset-2" />
      </Field>)}
    </div>
    <Field label="人物缩放" hint="1× 先按画布适配；X 正数向右，Y 正数向下。数值输入可超出滑杆范围。">
      <input type="range" min={.25} max={2} step={.05} value={Math.max(.25, Math.min(2, value.scale))} disabled={disabled}
        onChange={event => update("scale", event.target.valueAsNumber)} className="w-full accent-neutral-700" />
    </Field>
    <div className="flex flex-wrap gap-2">
      <Button disabled={disabled || !changed} onClick={onApply}>Apply · 保存位置版本</Button>
      <Button variant="outline" disabled={disabled || !selectedVersion} onClick={onUndo}>Undo · 上一版本</Button>
      <Button variant="ghost" disabled={disabled} onClick={() => onChange({ x: 0, y: 0, scale: 1 })}>居中 / 1×</Button>
    </div>
    <p className="text-xs leading-5 text-muted-foreground">Apply 只登记新位置版本，引用原 RGB 与 Alpha。Undo 与恢复不会重新抠像，也不会覆盖历史文件。</p>
    {versions.length > 0 && <div className="border-t pt-5">
      <h3 className="mb-3 text-xs font-medium">位置版本</h3>
      <div className="max-h-48 space-y-2 overflow-auto">
        {[...versions].reverse().map(version => <div key={version.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-muted/50 px-3 py-2 text-xs">
          <div className="min-w-0"><p className="font-mono tabular-nums">X {version.x} · Y {version.y} · {version.scale}×</p>
            <p className="mt-1 text-[10px] text-muted-foreground">{new Date(version.created_at).toLocaleString("zh-CN")}</p></div>
          <Button size="xs" variant="outline" disabled={disabled || version.person_asset_id === currentPersonAssetId} onClick={() => onRestore(version.person_asset_id)}>
            {version.person_asset_id === currentPersonAssetId ? "当前版本" : "恢复"}
          </Button>
        </div>)}
      </div>
    </div>}
  </div>;
}

export function PersonTransformReview({ task, person, backgrounds = [], width, height, disabled, onChanged }: { task: Task; person: Asset; backgrounds?: Asset[]; width?: number; height?: number; disabled?: boolean; onChanged: (personAssetId: string) => void | Promise<unknown> }) {
  const path = `/api/manual-compose/tasks/${encodeURIComponent(task.id)}/transforms`;
  const versions = useQuery({ queryKey: ["manual-transforms", task.id], queryFn: () => request<{ versions: TransformVersion[] }>(path), refetchInterval: 2000, retry: false });
  const transform = person.legacy_reference?.transform as Partial<PersonTransform> | undefined;
  const saved = { x: transform?.x ?? 0, y: transform?.y ?? 0, scale: transform?.scale ?? 1 };
  const [value, setValue] = useState<PersonTransform>(saved);
  const [message, setMessage] = useState("");
  const mutation = useMutation({ mutationFn: ({ action, personAssetId }: { action: "apply" | "undo" | "restore"; personAssetId?: string }) =>
    request<{ person: Asset; message: string }>(path + (action === "apply" ? "" : "/" + action), { method: "POST",
      body: action === "apply" ? JSON.stringify({ person_asset_id: person.id, ...value }) : action === "restore" ? JSON.stringify({ person_asset_id: personAssetId }) : undefined,
      signal: AbortSignal.timeout(120000) }), onSuccess: async response => { setMessage(response.message); await versions.refetch(); await onChanged(response.person.id); } });
  return <div className="space-y-4">
    <PersonPositionPreview taskId={task.id} person={person} backgrounds={backgrounds} width={width} height={height} value={value} onChange={setValue} disabled={disabled || mutation.isPending} />
    <TransformControls value={value} onChange={setValue} onApply={() => mutation.mutate({ action: "apply" })} onUndo={() => mutation.mutate({ action: "undo" })} onRestore={id => mutation.mutate({ action: "restore", personAssetId: id })}
      versions={versions.data?.versions || []} currentPersonAssetId={person.id} disabled={disabled || mutation.isPending} changed={JSON.stringify(value) !== JSON.stringify(saved)} />
    <div aria-live="polite" className="text-xs">{mutation.isPending && <p className="text-muted-foreground">正在校验母资产并保存版本…</p>}{message && <p>{message}</p>}{mutation.isError && <p role="alert" className="text-red-600">{mutation.error.message}</p>}{versions.isError && <p role="alert" className="text-red-600">位置版本读取失败：{versions.error.message}</p>}</div>
  </div>;
}
