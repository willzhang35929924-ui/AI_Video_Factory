"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { request } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/factory-ui";

type Description = {id:string;text:string;weight:number;enabled:boolean};
type Identity = {product_id:string;name:string;country:string;keyword:string;product_image_sha256:string;
  input_sha256:string;pool_sha256:string;source_context:Record<string,unknown>};
type Source = Identity & {descriptions:Description[];content_review:string};
type Options = {target:Identity;sources:Source[];target_has_descriptions:boolean};

export function DescriptionReuse({productId,onReady}:{productId:string;onReady:(descriptions:Description[])=>void}) {
  const client=useQueryClient(),[sourceId,setSourceId]=useState(""),[confirmed,setConfirmed]=useState(false),
    [busy,setBusy]=useState(false),[error,setError]=useState(""),[message,setMessage]=useState("");
  const options=useQuery({queryKey:["publishing","description-reuse",productId],
    queryFn:()=>request<Options>(`/api/publishing/products/${encodeURIComponent(productId)}/descriptions/reuse-options`)});
  const source=options.data?.sources.find(row=>row.product_id===sourceId);
  async function reuse(){
    if(!source||!options.data||!confirmed)return;
    setBusy(true);setError("");setMessage("");
    try{
      const result=await request<{product:{descriptions:Description[]};reused:boolean}>(`/api/publishing/products/${encodeURIComponent(productId)}/descriptions/reuse`,
        {method:"POST",body:JSON.stringify({source_product_id:source.product_id,source_pool_sha256:source.pool_sha256,
          source_input_sha256:source.input_sha256,target_input_sha256:options.data.target.input_sha256,confirmed:true})});
      onReady(result.product.descriptions);
      await client.invalidateQueries({queryKey:["publishing"]});
      setConfirmed(false);setSourceId("");
      setMessage("20条已有文案已保存，仍需核验产品事实。未调用AI、未改云文档、未复制商品ID；发布时另行确认商品。");
    }catch(e){setError(e instanceof Error?e.message:"文案复用未保存");}finally{setBusy(false);}
  }
  return <section className="space-y-3 rounded-lg border p-3" aria-label="复用已有产品文案">
    <div className="flex items-center justify-between gap-3"><p className="text-xs font-medium">复用已有产品文案</p>
      <Button variant="ghost" size="sm" disabled={busy} onClick={()=>{setSourceId("");setConfirmed(false);void options.refetch();}}>刷新文案来源</Button></div>
    <p className="text-xs leading-5 text-muted-foreground">仅列出国家、精确关键词和实物图SHA一致的20条已保存文案。来源行不同也须明确选择，不会自动覆盖。先保存产品编辑，再复用。</p>
    {options.data&&<p className="text-xs leading-5 text-muted-foreground">当前已保存目标：{options.data.target.country} · {options.data.target.keyword} · {options.data.target.product_id}</p>}
    {options.isPending?<p role="status" className="text-xs">正在核对真实产品图和文案…</p>:options.data?.target_has_descriptions?
      <p className="text-xs text-muted-foreground">本产品已有文案，复用入口不会覆盖。请在现有内容核验入口审阅当前文本。</p>:options.data&&!options.data.sources.length?
      <p className="text-xs text-muted-foreground">没有身份一致的20条文案池。请补全实物图和唯一关键词，或使用已配置的文案生成入口。</p>:options.data&&<>
      <Field label="选择文案来源"><select aria-label="选择已有文案池" className="h-9 w-full rounded-lg border bg-background px-3 text-xs"
        disabled={busy} value={sourceId} onChange={e=>{setSourceId(e.target.value);setConfirmed(false);setError("");setMessage("");}}>
        <option value="">请选择来源，不自动匹配</option>{options.data.sources.map(row=><option key={row.product_id} value={row.product_id}>{row.name} · {row.country} · {row.product_id} · 20条</option>)}</select></Field>
      {source&&<><p className="break-all text-[10px] leading-5 text-muted-foreground">来源：{source.product_id} · 行 {String(source.source_context.source_row??"未配置")}<br/>
        文案SHA：{source.pool_sha256}<br/>产品图SHA：{source.product_image_sha256} · 来源内容核验：{source.content_review}（不会继承）</p>
        <details><summary className="cursor-pointer text-xs">查看20条已有文案</summary><div className="max-h-52 space-y-2 overflow-y-auto pt-3">{source.descriptions.map(row=><p className="text-xs leading-5" key={row.id}>{row.id} · {row.text}</p>)}</div></details>
        <label className="flex items-start gap-2 text-xs leading-5"><input className="mt-1" aria-label="确认复用所选文案池" type="checkbox" disabled={busy}
          checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>确认仅复用这个产品的已有文本，之后再核验事实；不迁移来源行或商品绑定。</label></>}
      <Button size="sm" variant="outline" disabled={busy||!source||!confirmed} onClick={reuse}>{busy?"保存复用文案中…":"复用20条已有文案"}</Button>
    </>}
    {(error||options.error)&&<p role="alert" className="text-xs text-red-600">{error||options.error?.message}</p>}
    {message&&<p role="status" className="text-xs leading-5">{message}</p>}
  </section>;
}
