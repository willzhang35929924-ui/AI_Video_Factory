import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/providers";
import { AppShell } from "@/components/app-shell";

export const metadata: Metadata = {
  title: "AI Video Factory",
  description: "任务驱动的 AI 短视频生产工作台",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="zh-CN"
      className="h-full antialiased font-sans"
      suppressHydrationWarning
    >
      <body className="min-h-full"><Providers><AppShell>{children}</AppShell></Providers></body>
    </html>
  );
}
