export const dynamic = "force-dynamic";

export function GET() {
  const apiBase = process.env.NEXT_PUBLIC_API_URL || "/factory-api";
  const sameOrigin = apiBase === "/factory-api";
  const api = new URL(sameOrigin ? process.env.FACTORY_API_ORIGIN || "http://127.0.0.1:8000" : apiBase);
  return Response.json({ application: "AI Video Factory", api_origin: api.origin,
    api_base_path: sameOrigin ? apiBase : api.pathname,
    api_transport: sameOrigin ? "same-origin" : "direct" });
}
