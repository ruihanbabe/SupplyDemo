import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SupplyAgent",
  description: "AI Native CRM 与供应链处置系统",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
