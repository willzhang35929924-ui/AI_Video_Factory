"use client";

import { use, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { ArrowLeft, ArrowUpRight, Clock3, KeyRound, Save, ShieldCheck, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState, FactoryGuard, Field, PageHeading, Panel, SmallMetric, StatusBadge, Thumbnail } from "@/components/factory-ui";
import { API_URL, request, useFactory } from "@/lib/api";
import { useNotice } from "@/components/providers";
import { LocalAccountForm } from "@/components/local-account-form";
import { timeLabel, type PublishingAccount, type Product, type PublishingTask } from "@/lib/types";

type AccountStatus="NOT_CONNECTED"|"WAITING_MANUAL"|"EXPIRED"|"DISABLED";
type PublishRun={id:string;attempt:number;status:string;started_at:string|null;finished_at:string|null;last_heartbeat:string|null;error_code:string|null;error_message:string|null;logs:unknown[]};
type PublishStep={id:string;name:string;status:string;started_at:string|null;finished_at:string|null;error_code:string|null;error_message:string|null};
type PublishDetail=PublishingTask&{runs:PublishRun[];steps:PublishStep[]};

export default function AccountDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const factory = useFactory();
  const query=useQueryClient();
  const accounts=useQuery({queryKey:["publishing","accounts"],queryFn:()=>request<PublishingAccount[]>("/api/publishing/accounts"),refetchInterval:3000});
  const catalog=useQuery({queryKey:["publishing","products"],queryFn:()=>request<Product[]>("/api/publishing/products"),refetchInterval:3000});
  const tasks=useQuery({queryKey:["publishing","tasks"],queryFn:()=>request<PublishingTask[]>("/api/publishing/tasks"),refetchInterval:3000});
  const notice = useNotice();
  const [authorizeOpen, setAuthorizeOpen] = useState(false);
  const [statusDrafts, setStatusDrafts] = useState<Record<string, AccountStatus>>({});
  const [runTaskId,setRunTaskId]=useState("");
  const history=useQuery({queryKey:["publishing","task-history",runTaskId],queryFn:()=>request<PublishDetail>(`/api/publishing/tasks/${encodeURIComponent(runTaskId)}`),enabled:!!runTaskId,refetchInterval:q=>q.state.data?.runs.some(run=>run.status==="RUNNING")?2000:false});
  const [busy,setBusy]=useState(false);
  const data = factory.data;
  const account = accounts.data?.find((item) => item.id === id);
  const publishing = tasks.data?.filter((item) => item.account_id === id) || [];
  const taskIds = new Set(publishing.map((item) => item.task_id));
  const publishingIds = new Set(publishing.map((item) => item.id));
  const products = catalog.data?.filter((item) => account?.product_ids.includes(item.id)) || [];
  const errors = data?.errors.filter((item) => taskIds.has(item.task_id) && item.stage === "publish") || [];
  const audits = data?.audits.filter((item) => item.entity_id === id || publishingIds.has(item.entity_id) || (!!item.task_id && taskIds.has(item.task_id) && item.action.startsWith("publish"))) || [];
  const localStatus:AccountStatus=account?.enabled===false?"DISABLED":account?.legacy_status==="EXPIRED"?"EXPIRED":account?.legacy_status==="WAITING_MANUAL"?"WAITING_MANUAL":"NOT_CONNECTED";
  const draft = statusDrafts[id] ?? localStatus;
  const missing=account?[...(!account.expected_username?["TikTok 用户名未配置"]:[]),...(!account.hubstudio_env_id?["HubStudio 环境编号未配置"]:[]),...(!account.cdp_endpoint?["已有浏览器 CDP 未连接"]:[]),...(account.identity_status!=="OBSERVED"?["真实 TikTok 身份尚未核实"]:[]),...(account.network_status!=="OBSERVED"?["VPN 出口国家尚未核实"]:[])]:[];

  async function change() {
    if (!account || busy) return;
    setBusy(true);
    try {
      await request(`/api/publishing/accounts/${encodeURIComponent(id)}`,{method:"PUT",body:JSON.stringify({name:account.name,country:account.country,status:draft,product_ids:account.product_ids,enabled:draft==="DISABLED"?false:account.enabled,hubstudio_env_id:account.hubstudio_env_id,cdp_endpoint:account.cdp_endpoint,expected_username:account.expected_username,cooldown_seconds:account.cooldown_seconds,publishing_context:account.publishing_context})});
      await query.invalidateQueries({queryKey:["publishing"]});await factory.refresh();notice("账号状态已保存；登录与网络状态仍以实际检测为准");setStatusDrafts(current=>{const updated={...current};delete updated[id];return updated;});
    } catch(error){notice(error instanceof Error?error.message:"保存失败");} finally { setBusy(false); }
  }

  async function toggleEnabled() {
    if (!account || busy) return;
    setBusy(true);
    try {
      await request(`/api/publishing/accounts/${encodeURIComponent(id)}`,{method:"PATCH",body:JSON.stringify({enabled:!account.enabled})});
      await query.invalidateQueries({queryKey:["publishing"]});await factory.refresh();setStatusDrafts(current=>{const updated={...current};delete updated[id];return updated;});notice(account.enabled?"后续调度已暂停":"后续调度已启用；仍须真实就绪检查，不自动发布");
    } catch(error){notice(error instanceof Error?error.message:"启停失败");} finally { setBusy(false); }
  }

  if(accounts.isPending)return <Panel><p role="status" className="text-xs">正在读取真实账号资料…</p></Panel>;
  if(accounts.isError)return <Panel title="账号资料无法读取"><p role="alert" className="text-xs text-red-600">{accounts.error.message}</p><Button variant="outline" className="mt-4" onClick={()=>accounts.refetch()}>重新读取账号</Button></Panel>;
  return <FactoryGuard factory={factory}>{!account ? <EmptyState title="账号不存在" description="此本地资料可能已删除，请返回账号管理查看现有记录。"><Link href="/accounts" className="text-xs underline underline-offset-4">返回账号管理</Link></EmptyState> : <>
    <Link href="/accounts" className="mb-6 inline-flex items-center gap-2 text-xs text-muted-foreground"><ArrowLeft size={13} />返回账号管理</Link>
    {(catalog.isError||tasks.isError)&&<Panel title="关联数据未能读取"><p role="alert" className="text-xs text-red-600">{catalog.error?.message||tasks.error?.message}</p><Button variant="outline" className="mt-4" onClick={()=>{void catalog.refetch();void tasks.refetch();}}>重新读取关联数据</Button></Panel>}
    <PageHeading eyebrow={account.id} title={account.name} description={`${account.country} · 本地资料、产品关联与实际运行历史`}><Button disabled={busy} onClick={() => setAuthorizeOpen(true)}><KeyRound size={14} />编辑本地资料</Button></PageHeading>
    <div className="mb-7 grid gap-6 rounded-xl border p-6 sm:grid-cols-4"><div><p className="mb-3 text-[11px] text-muted-foreground">当前状态</p><StatusBadge status={account.status} /></div><SmallMetric label="当前队列" value={tasks.data?publishing.filter(item=>["QUEUED","RUNNING"].includes(item.status)).length:"UNKNOWN"} /><SmallMetric label="关联产品" value={catalog.data?products.length:"UNKNOWN"} /><SmallMetric label="环境连接" value={account.connection_verified?"已核实":"未核实"} /></div>
    <div className="grid items-start gap-7 xl:grid-cols-[minmax(0,1fr)_330px]">
      <div className="min-w-0 space-y-7">
        <Panel title="关联产品" description="本地账号与同国家产品的真实关联，不代表外部商品授权。">{products.length ? <div className="grid gap-4 sm:grid-cols-2">{products.map(product=><Link href="/products" key={product.id} className="flex gap-4 rounded-lg border p-4 hover:bg-muted/30"><Thumbnail src={product.image?`${API_URL}/api/products/${product.id}/image`:""} alt={product.name} className="size-20 shrink-0" /><div className="min-w-0"><p className="text-xs font-medium leading-5">{product.name}</p><p className="mt-2 text-[10px] text-muted-foreground">{product.country}</p><p className="mt-2 break-all font-mono text-[9px] text-muted-foreground">{product.binding_reference||"NOT_CONFIGURED"}</p></div><ArrowUpRight size={13} className="ml-auto shrink-0" /></Link>)}</div> : <EmptyState title={catalog.isError?"关联产品读取失败":catalog.isPending?"正在读取产品":"尚无关联产品"} description="可在编辑本地资料中选择同国家产品。" />}</Panel>
        <Panel title="发布历史与队列" description={tasks.data?`${publishing.length} 条与此账号直接关联的发布记录`:"正在读取实际关联记录"} action={<Link href="/publish" className="inline-flex items-center gap-1 text-xs">发布中心<ArrowUpRight size={12} /></Link>}>
          {publishing.length ? <div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="border-b text-[10px] text-muted-foreground"><tr>{["视频任务","国家 / 产品","计划时间","状态 / 运行"].map(label=><th key={label} className="px-3 pb-3 font-normal first:pl-0">{label}</th>)}</tr></thead><tbody>{publishing.map(item=><tr key={item.id} className="border-b last:border-0"><td className="py-4 pr-3"><Link href={`/tasks/${item.task_id}`} className="font-medium hover:underline">{item.task_id}</Link><p className="mt-2 font-mono text-[9px] text-muted-foreground">{item.id}</p>{item.error&&<p className="mt-2 max-w-64 whitespace-normal text-[10px] text-red-600 dark:text-red-400">{item.error}</p>}</td><td className="px-3"><p>{item.country}</p><p className="mt-2 text-[10px] text-muted-foreground">{catalog.data?.find(product=>product.id===item.product_id)?.name||item.product_id}</p></td><td className="px-3 text-[10px]">{timeLabel(item.schedule)}</td><td className="px-3"><StatusBadge status={item.status} /><Button variant="ghost" size="sm" className="mt-2 block" onClick={()=>setRunTaskId(item.id)}>查看运行记录</Button></td></tr>)}</tbody></table></div> : <EmptyState title={tasks.isError?"发布记录读取失败":tasks.isPending?"正在读取发布记录":"暂无发布记录"} description="这里只显示真实关联任务，不填充示例发布历史。" />}
        </Panel>
        <Panel title="账号操作历史" description="仅显示此账号和直接关联发布记录的审计信息。" action={<Clock3 size={15} className="text-muted-foreground" />}>{audits.length ? <ol className="space-y-5">{[...audits].reverse().map(item=><li key={item.id} className="flex gap-3"><span className="mt-2 size-1 shrink-0 rounded-full bg-muted-foreground" /><div><p className="text-xs leading-6">{item.message}</p><p className="mt-1 text-[10px] text-muted-foreground">{timeLabel(item.created_at)} · {item.action}</p></div></li>)}</ol> : <p className="text-xs leading-6 text-muted-foreground">尚无可读取的审计记录。实际任务的 Run 和 Step 历史在“查看运行记录”中。</p>}</Panel>
      </div>
      <aside className="space-y-7">
        <Panel title="实际连接状态" action={<ShieldCheck size={16} className="text-muted-foreground" />}><StatusBadge status={account.status} /><dl className="mt-5 space-y-4 text-[11px]">{[["目标市场",account.country],["TikTok 用户名",account.expected_username||"NOT_CONFIGURED"],["HubStudio 环境",account.hubstudio_env_id||"NOT_CONFIGURED"],["浏览器 CDP",account.cdp_endpoint||"NOT_CONNECTED"],["身份检测",account.identity_status],["网络检测",account.network_status],["最近发布时间",timeLabel(account.last_publish)]].map(([label,value])=><div key={label}><dt className="text-muted-foreground">{label}</dt><dd className="mt-2 break-all">{value}</dd></div>)}</dl>{missing.length>0&&<div role="status" className="mt-5 border-t pt-4"><p className="mb-2 text-xs">当前不能发布的原因</p>{missing.map(reason=><p key={reason} className="text-[10px] leading-5 text-muted-foreground">· {reason}</p>)}</div>}<p className="mt-5 border-t pt-4 text-[10px] leading-5 text-muted-foreground">保存资料不会登录或发布。请在已有环境完成人工登录；发布任务的真实就绪检查核实身份与出口后才会更新连接状态。本页不保存密码、Cookie 或 Token。</p></Panel>
        <Panel title="本地状态管理" description="资料状态与启停独立；启用不代表登录已验证。"><Field label="本地账号状态"><select aria-label="详情本地账号状态" className="h-9 rounded-lg border bg-background px-3 text-xs" disabled={busy} value={draft} onChange={event=>setStatusDrafts(current=>({...current,[id]:event.target.value as AccountStatus}))}><option value="NOT_CONNECTED">未连接</option><option value="WAITING_MANUAL">等待人工登录 / 授权</option><option value="EXPIRED">用户标记已过期</option><option value="DISABLED">停用后续调度</option></select></Field><Button variant="outline" className="mt-5 w-full" disabled={busy||draft===localStatus} onClick={change}><Save size={14}/>{busy?"正在保存":"保存本地状态"}</Button><Button variant="outline" className="mt-3 w-full" disabled={busy} onClick={toggleEnabled}>{account.enabled?"暂停后续调度":"启用后续调度"}</Button><p className="mt-3 text-[10px] leading-5 text-muted-foreground">{account.enabled?"当前允许后续调度；执行前仍需核实实际登录和网络。":"当前后续调度已停用。更改资料状态不会自动启用，请明确点击“启用后续调度”。"} 本地变更不启动发布。</p></Panel>
        <Panel title="错误记录" action={<TriangleAlert size={15} className="text-muted-foreground" />}>{account.errors.length||errors.length ? <div className="space-y-4">{account.errors.map((error,index)=><p key={`${index}-${error}`} className="text-xs leading-6 text-red-600 dark:text-red-400">{error}</p>)}{errors.map(error=><div key={error.id} className="border-t pt-4"><p className="text-xs font-medium">{error.code}</p><p className="mt-2 text-[11px] leading-5 text-muted-foreground">{error.message}</p><p className="mt-2 text-[10px] text-muted-foreground">{error.task_id} · {timeLabel(error.created_at)}</p></div>)}</div> : <p className="text-xs leading-6 text-muted-foreground">此账号没有已记录的关联错误。</p>}</Panel>
      </aside>
    </div>
    {authorizeOpen&&<LocalAccountForm account={account} products={catalog.data||[]} onClose={()=>setAuthorizeOpen(false)} onSaved={()=>{void query.invalidateQueries({queryKey:["publishing"]});void factory.refresh();notice("本地账号资料已保存");}}/>}
    <Dialog open={!!runTaskId} onOpenChange={open=>{if(!open)setRunTaskId("");}}><DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-3xl"><DialogHeader><DialogTitle>实际运行记录</DialogTitle><DialogDescription>{runTaskId} · 只读 Run / Step 历史，不触发上传或发布。</DialogDescription></DialogHeader>
      {history.isPending&&<p role="status" className="text-xs">正在读取运行记录…</p>}
      {history.isError&&<div><p role="alert" className="text-xs text-red-600">{history.error.message}</p><Button variant="outline" className="mt-4" onClick={()=>history.refetch()}>重新读取运行记录</Button></div>}
      {history.data&&<div className="space-y-6"><section><h3 className="mb-3 text-xs font-medium">Run</h3>{history.data.runs.length?history.data.runs.map(run=><div key={run.id} className="mb-4 rounded-lg border p-4 text-xs"><p className="break-all font-mono text-[10px]">{run.id} · attempt {run.attempt}</p><p className="my-3"><StatusBadge status={run.status} /></p><p className="text-[10px] leading-6 text-muted-foreground">开始 {timeLabel(run.started_at)} · 结束 {timeLabel(run.finished_at)} · heartbeat {timeLabel(run.last_heartbeat)}</p>{run.error_code&&<p className="mt-3 text-red-600">{run.error_code} · {run.error_message}</p>}{!!run.logs?.length&&<pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-muted p-3 text-[10px]">{run.logs.map(log=>typeof log==="string"?log:JSON.stringify(log)).join("\n")}</pre>}</div>):<p className="text-xs text-muted-foreground">此任务尚未产生 PublishRun；不显示虚构成功记录。</p>}</section><section><h3 className="mb-3 text-xs font-medium">Step</h3>{history.data.steps.length?history.data.steps.map(step=><div key={step.id} className="mb-3 rounded-lg border p-3 text-xs"><p>{step.name} · {step.status}</p><p className="mt-2 text-[10px] text-muted-foreground">{timeLabel(step.started_at)} – {timeLabel(step.finished_at)}</p>{step.error_code&&<p className="mt-2 text-red-600">{step.error_code} · {step.error_message}</p>}</div>):<p className="text-xs text-muted-foreground">尚无持久化步骤记录。</p>}</section></div>}
    </DialogContent></Dialog>
  </>}</FactoryGuard>;
}
