import { createSupabaseServerClient } from "@/lib/supabase/server";

const FORWARDED_HEADERS = ["content-type", "idempotency-key", "last-event-id", "x-request-id"];

function apiBaseUrl(): string | null {
  const value = process.env.SUPPLYAGENT_API_BASE_URL?.replace(/\/$/, "");
  return value || null;
}

async function accessToken(request: Request): Promise<string | null> {
  const supplied = request.headers.get("authorization");
  if (supplied?.toLowerCase().startsWith("bearer ")) return supplied.slice("bearer ".length);
  try {
    const supabase = await createSupabaseServerClient();
    const { data } = await supabase.auth.getSession();
    return data.session?.access_token ?? null;
  } catch {
    return null;
  }
}

function unavailableResponse() {
  return Response.json(
    { ok: false, error: { code: "product_api_unavailable", message: "产品 API 未配置" } },
    { status: 503 },
  );
}

// Next BFF 只转发白名单头、原始请求体和 Supabase access token；Python 重新验证 token 与组织关系。
export async function proxyProductRequest(request: Request): Promise<Response> {
  const baseUrl = apiBaseUrl();
  if (!baseUrl) return unavailableResponse();
  const headers = new Headers();
  for (const name of FORWARDED_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const token = await accessToken(request);
  if (token) headers.set("authorization", `Bearer ${token}`);
  const hasBody = !["GET", "HEAD"].includes(request.method);
  const url = new URL(request.url);
  let upstream: Response;
  try {
    upstream = await fetch(`${baseUrl}${url.pathname.replace(/^\/api/, "")}${url.search}`, {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      cache: "no-store",
    });
  } catch {
    return Response.json(
      { ok: false, error: { code: "product_api_unreachable", message: "产品 API 不可达" } },
      { status: 502 },
    );
  }
  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.delete("connection");
  responseHeaders.delete("transfer-encoding");
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
}

// 健康检查不附带会话，也不要求 Python 连接数据库。
export async function proxyHealthRequest(request: Request): Promise<Response> {
  const baseUrl = apiBaseUrl();
  if (!baseUrl) return unavailableResponse();
  return fetch(`${baseUrl}/health`, { method: request.method, cache: "no-store" });
}
