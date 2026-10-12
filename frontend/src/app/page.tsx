export default function HomePage() {
  return (
    <main>
      <h1>SupplyAgent</h1>
      <p>AI Native CRM 与供应链处置系统。</p>
      <section className="notice">
        <strong>F31 基础设施正在建立。</strong>
        <p>此页面不读取业务数据、不连接数据库，也不代表 CRM、Agent 或审批功能已经实现。</p>
        <p>后续所有业务访问将由服务端 <code>Route Handler</code> 经身份、组织与 RLS 边界处理。</p>
      </section>
    </main>
  );
}
