import { proxyProductRequest } from "@/lib/bff/proxy";

// F39 只保留同源转发；供应链规则、数据库和授权均在 Python。
export {
  proxyProductRequest as GET,
  proxyProductRequest as POST,
  proxyProductRequest as PUT,
};
