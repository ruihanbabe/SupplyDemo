"use client";

import { createBrowserClient } from "@supabase/ssr";
import { publicSupabaseEnvironment } from "./env";

// 浏览器客户端只能使用 publishable key，RLS 仍由数据库强制执行。
export function createSupabaseBrowserClient() {
  const { url, publishableKey } = publicSupabaseEnvironment();
  return createBrowserClient(url, publishableKey);
}
