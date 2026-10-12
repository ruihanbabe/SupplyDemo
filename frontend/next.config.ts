import type { NextConfig } from "next";

// F31：浏览器只能访问 Route Handler；不在构建期暴露任何 service-role 凭证。
const nextConfig: NextConfig = {
  reactStrictMode: true,
};

export default nextConfig;
