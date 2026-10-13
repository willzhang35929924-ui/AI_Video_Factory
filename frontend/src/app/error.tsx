"use client";
import { Button } from "@/components/ui/button";
export default function ErrorPage({reset}:{error:Error;reset:()=>void}){return <section className="rounded-xl border p-10"><h1 className="text-xl font-semibold">页面未能正常加载</h1><p className="my-5 text-sm text-muted-foreground">请重试加载，已保存的任务记录会保留。</p><Button onClick={reset}>重新加载</Button></section>;}
