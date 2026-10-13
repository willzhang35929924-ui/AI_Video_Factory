import type { NextConfig } from "next";

const backend = new URL(process.env.FACTORY_API_ORIGIN || "http://127.0.0.1:8000");
if (!['127.0.0.1', 'localhost'].includes(backend.hostname) || backend.protocol !== 'http:'
  || backend.username || backend.password || backend.pathname !== '/' || backend.search || backend.hash) {
  throw new Error("FACTORY_API_ORIGIN must be a local HTTP origin; never expose an arbitrary proxy target");
}

const nextConfig: NextConfig = {
  distDir: process.env.NEXT_BUILD_DIR || ".next",
  poweredByHeader: false,
  devIndicators: false,
  experimental: { proxyTimeout: 120000 },
  async rewrites() {
    return [{ source: "/factory-api/:path*", destination: `${backend.origin}/:path*` }];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
