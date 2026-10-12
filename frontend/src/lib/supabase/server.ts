import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";
import { publicSupabaseEnvironment } from "./env";

// Route Handler / Server Component 通过 cookie 恢复会话；service role 不参与用户请求认证。
export async function createSupabaseServerClient() {
  const cookieStore = await cookies();
  const { url, publishableKey } = publicSupabaseEnvironment();
  return createServerClient(url, publishableKey, {
    cookies: {
      getAll: () => cookieStore.getAll(),
      setAll: (entries) => {
        try {
          entries.forEach(({ name, value, options }) => cookieStore.set(name, value, options));
        } catch {
          // Server Component 不能写 cookie；刷新会话仅在 Route Handler / Proxy 中进行。
        }
      },
    },
  });
}
