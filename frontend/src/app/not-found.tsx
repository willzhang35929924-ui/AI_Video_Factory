import Link from "next/link";
export default function NotFound(){return <section className="rounded-xl border p-10"><h1 className="text-xl font-semibold">没有找到这条记录</h1><p className="my-5 text-sm text-muted-foreground">记录可能已经移除，或链接地址有误。</p><Link href="/dashboard" className="text-sm underline underline-offset-4">返回生产总览</Link></section>;}
