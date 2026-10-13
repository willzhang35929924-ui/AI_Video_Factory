"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { createContext, useContext, useState } from "react";
import { X } from "lucide-react";
const NoticeContext = createContext<(message: string) => void>(() => {});
export function useNotice() { return useContext(NoticeContext); }
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { staleTime: 1000, retry: 1 } } }));
  const [notice, setNotice] = useState("");
  return <QueryClientProvider client={client}><ThemeProvider attribute="class" defaultTheme="light" enableSystem><NoticeContext.Provider value={setNotice}>{children}{notice && <div role="status" className="pointer-events-none fixed bottom-6 right-6 z-50 flex max-w-sm items-center gap-4 rounded-xl border bg-background px-5 py-4 text-sm shadow-sm">{notice}<button className="pointer-events-auto" aria-label="关闭提示" onClick={() => setNotice("")}><X size={16} /></button></div>}</NoticeContext.Provider></ThemeProvider></QueryClientProvider>;
}
