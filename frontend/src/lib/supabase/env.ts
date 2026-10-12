type PublicSupabaseEnvironment = {
  url: string;
  publishableKey: string;
};

// 只读取浏览器允许知道的两个公开配置；service role 永远不从这里返回。
export function publicSupabaseEnvironment(): PublicSupabaseEnvironment {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const publishableKey = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (!url || !publishableKey) {
    throw new Error("缺少 NEXT_PUBLIC_SUPABASE_URL 或 NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY");
  }
  return { url, publishableKey };
}
