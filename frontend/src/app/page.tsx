"use client";

import Link from "next/link";
import { FormEvent, useEffect, useRef, useState } from "react";
import { createSupabaseBrowserClient } from "@/lib/supabase/browser";

type Organization = { organizationId: string; displayName: string };
type Opportunity = { id: string; title: string; customer: string; stage: string };
type Claim = { conclusion: string; quality: string; citations: { sourceId: string; fragmentHash: string }[]; unknowns: string[]; conflicts: string[] };
type RunEvent = { eventId: string; sequenceNo: number; eventType: string; redactedSummary: string; occurredAt: string };
type Usage = { costMicros: number | null; usageState: string; usage: { input_tokens: number; output_tokens: number }; attempts: { invocationId: string; role: string; status: string; elapsedSeconds: number | null }[] };
type Investigation = {
  runId: string; question: string; status: string; generation: number; mode: string;
  errorCode: string | null; issues: string[]; review: string;
  result: { claims: Claim[]; unknowns: string[]; conflicts: string[] } | null;
  steps: { id: string; role: string; status: string; generation: number }[];
  evidence: { id: string; tool_name: string; observed_at: string; excerpt: string }[];
};
const roleNames: Record<string, string> = { customer_facts: "客户事实", business_policy: "业务知识", supply_risk: "供应风险", opportunity_diagnosis: "商机回答" };
const statusNames: Record<string, string> = { queued: "等待 Worker 领取", leased: "正在调查", completed: "调查结束，待审阅", failed: "调查失败", cancelled: "已取消", running: "进行中" };

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1/${path}`, { ...init, cache: "no-store", headers: { "content-type": "application/json", ...init?.headers } });
  const body = await response.json();
  if (!response.ok || !body.ok) throw new Error(body.error?.message || `请求失败 (${response.status})`);
  return body.data;
}

export default function HomePage() {
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [organization, setOrganization] = useState("");
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [loadedOrganization, setLoadedOrganization] = useState("");
  const pendingSubmission = useRef<{ signature: string; key: string } | null>(null);
  const [opportunity, setOpportunity] = useState("");
  const [question, setQuestion] = useState("");
  const [runId, setRunId] = useState("");
  const [investigation, setInvestigation] = useState<Investigation | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authenticated, setAuthenticated] = useState(false);
  const [reload, setReload] = useState(0);
  const [retry, setRetry] = useState(0);
  const [timeline, setTimeline] = useState<{ key: string; events: RunEvent[] }>({ key: "", events: [] });
  const [streamStatus, setStreamStatus] = useState("");
  const [usage, setUsage] = useState<{ key: string; data: Usage } | null>(null);
  const streamKey = `${organization}/${runId}`;

  useEffect(() => {
    if (!runId || !organization) return;
    const source = new EventSource(`/api/v1/intelligence/runs/${runId}/events?organization_id=${encodeURIComponent(organization)}`);
    source.onopen = () => {
      setTimeline(previous => previous.key === streamKey ? previous : { key: streamKey, events: [] });
      setStreamStatus("实时事件已连接");
    };
    source.addEventListener("trace", (event: MessageEvent) => {
      try {
        const item = JSON.parse(event.data) as RunEvent;
        if (!item.eventId || !Number.isInteger(item.sequenceNo)) return;
        setTimeline(previous => {
          const events = previous.key === streamKey ? previous.events : [];
          if (events.some(e => e.eventId === item.eventId)) return previous;
          return { key: streamKey, events: [...events, item].sort((a, b) => a.sequenceNo - b.sequenceNo).slice(-500) };
        });
      } catch { setStreamStatus("事件格式无效"); source.close(); }
    });
    source.addEventListener("complete", () => { setStreamStatus("运行已结束"); source.close(); });
    source.addEventListener("access_revoked", () => {
      setTimeline({ key: streamKey, events: [] }); setUsage(null);
      setInvestigation(null); setError("访问权限已变化，请重新读取");
      setStreamStatus("访问已关闭"); source.close();
    });
    source.onerror = () => setStreamStatus("连接中断，正在重连；结果仍由轮询读取");
    return () => source.close();
  }, [runId, organization, streamKey, retry]);

  useEffect(() => {
    if (!runId || !organization) return;
    const controller = new AbortController();
    api<Usage>(`intelligence/runs/${runId}/evaluation?organization_id=${organization}`, { signal: controller.signal })
      .then(data => setUsage({ key: streamKey, data }))
      .catch(() => { if (!controller.signal.aborted) setUsage(null); });
    return () => controller.abort();
  }, [runId, organization, streamKey, investigation?.status, investigation?.generation, retry]);

  useEffect(() => {
    const controller = new AbortController();
    api<{ organizations: Organization[]; selectedOrganization: Organization | null }>("session", { signal: controller.signal })
      .then(data => {
        setAuthenticated(true);
        setOrganizations(data.organizations);
        const params = new URLSearchParams(window.location.search);
        const saved = data.organizations.find(o => o.organizationId === params.get("organization"));
        setOrganization(saved?.organizationId || data.selectedOrganization?.organizationId || data.organizations[0]?.organizationId || "");
        if (saved) setRunId(params.get("run") || "");
        setError("");
      }).catch(e => { if (!controller.signal.aborted) setError(e.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [reload]);

  useEffect(() => {
    if (!organization) return;
    const controller = new AbortController();
    api<Opportunity[]>(`intelligence/opportunities?organization_id=${organization}`, { signal: controller.signal })
      .then(data => { setOpportunities(data); setOpportunity(data[0]?.id || ""); setLoadedOrganization(organization); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [organization]);

  useEffect(() => {
    if (!runId || !organization) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const data = await api<Investigation>(`intelligence/runs/${runId}?organization_id=${organization}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setInvestigation(data);
        setError("");
        if (["queued", "leased"].includes(data.status)) timer = setTimeout(poll, 1500);
      } catch (e) {
        if (!controller.signal.aborted) {
          setInvestigation(null);
          setTimeline({ key: streamKey, events: [] }); setUsage(null);
          setError(e instanceof Error ? e.message : "读取失败");
        }
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, organization, streamKey, retry]);

  async function login(event: FormEvent) {
    event.preventDefault(); setError(""); setSubmitting(true);
    try {
      const result = await createSupabaseBrowserClient().auth.signInWithPassword({ email, password });
      if (result.error) throw result.error;
      setPassword(""); setReload(value => value + 1);
    } catch (e) { setError(e instanceof Error ? e.message : "登录失败"); }
    finally { setSubmitting(false); }
  }

  async function ask(event: FormEvent) {
    event.preventDefault();
    if (!question.trim() || !opportunity) return;
    setSubmitting(true); setError("");
    try {
      const signature = JSON.stringify([organization, opportunity, question.trim()]);
      if (pendingSubmission.current?.signature !== signature) pendingSubmission.current = { signature, key: crypto.randomUUID() };
      const data = await api<{ runId: string }>("intelligence/runs", { method: "POST", body: JSON.stringify({ organization_id: organization, opportunity_id: opportunity, question: question.trim(), idempotency_key: pendingSubmission.current.key, mode: "replay" }) });
      pendingSubmission.current = null;
      setInvestigation(null); setRunId(data.runId);
      window.history.replaceState(null, "", `?organization=${organization}&run=${data.runId}`);
    } catch (e) { setError(e instanceof Error ? e.message : "提交失败"); }
    finally { setSubmitting(false); }
  }
  const busy = submitting || (runId && (!investigation || ["queued", "leased"].includes(investigation.status)) && !error);

  const retainedIssues = investigation?.result ? [...investigation.result.unknowns, ...investigation.result.conflicts]
    .filter(item => !investigation.result?.claims.some(claim => [...claim.unknowns, ...claim.conflicts].includes(item))) : [];

  return <>
    <a className="skip-link" href="#main-content">跳至工作区</a>
    <header className="topbar"><Link className="brand" href="/">Supply<span>Agent</span></Link><span className="workspace-label">供应链业务工作台</span><span className="environment">本地 Replay</span></header>
    <main id="main-content" className="workspace">
      <aside className="sidebar">
        <p className="eyebrow">工作空间</p><h1>商机情报</h1><p className="muted intro">从客户事实到业务判断，每一个结论都有迹可循。</p>
        <nav aria-label="业务场景"><span className="nav-current" aria-current="page">情报问答 <span>已接通</span></span><p className="muted planned">后续开发：销售运营 · 客户沟通 · 主管巡检</p></nav>
        {loading ? <p role="status">正在核验会话…</p> : !authenticated ? <form onSubmit={login} className="question-form"><h2>登录工作空间</h2><label htmlFor="email">邮箱</label><input id="email" type="email" autoComplete="username" required value={email} onChange={e => setEmail(e.target.value)} /><label htmlFor="password">密码</label><input id="password" type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} /><button disabled={submitting} type="submit">{submitting ? "正在登录…" : "登录"}</button></form> : <form onSubmit={ask} className="question-form">
          <label htmlFor="organization">当前组织</label><select id="organization" value={organization} onChange={e => { setOrganization(e.target.value); setRunId(""); setInvestigation(null); setOpportunities([]); setOpportunity(""); setError(""); window.history.replaceState(null, "", "/"); }}>{organizations.map(o => <option key={o.organizationId} value={o.organizationId}>{o.displayName}</option>)}</select>
          {!organizations.length && <p className="muted">当前账号尚无组织成员关系。</p>}
          <label htmlFor="opportunity">调查商机</label><select id="opportunity" required value={opportunity} onChange={e => setOpportunity(e.target.value)} disabled={!opportunities.length}><option value="" disabled>选择有权访问的商机</option>{opportunities.map(o => <option key={o.id} value={o.id}>{o.customer} / {o.title}</option>)}</select>
          {organization && loadedOrganization === organization && !opportunities.length && <p className="muted">暂无可访问商机。请先录入业务数据或确认权限。</p>}
          <label htmlFor="question">你想了解什么？</label><textarea id="question" required maxLength={600} rows={6} value={question} onChange={e => setQuestion(e.target.value)} placeholder="例如：这个商机有哪些跟进缺口？依据是什么？" /><span className="input-note">{question.length}/600 · 仅调查当前商机</span>
          <button type="submit" disabled={Boolean(busy) || !opportunity || !question.trim()}>{busy ? "调查进行中…" : "开始调查 →"}</button><p className="muted planned">当前验证只读调查链路。不会更新 CRM 或发送消息。</p>
        </form>}
        {error && <div role="alert" className="error"><p>{error}</p><button type="button" className="secondary" onClick={() => runId ? setRetry(v => v + 1) : setReload(v => v + 1)}>重新读取</button></div>}
      </aside>
      <section className="answer-area" aria-labelledby="answer-title">
        <div className="section-heading"><div><p className="eyebrow">INVESTIGATION</p><h2 id="answer-title">调查与证据</h2></div><span className="mode-tag">只读 · 待审阅</span></div>
        {!runId ? <div className="empty-state"><span className="empty-mark" aria-hidden="true">↗</span><h3>把一个业务问题说清楚</h3><p>选择商机后，输入你需要确认的事实或判断。<br />调查会汇总客户、会话、任务与授权知识中的证据。</p><div className="flow-preview">客户事实 + 业务知识 <span>→</span> 商机回答 <span>→</span> 证据审阅</div></div> : <div aria-live="polite">
          <div className="run-heading"><h3>{investigation?.question || question || "正在恢复调查…"}</h3><p className="muted">{statusNames[investigation?.status || "queued"]} · 执行批次 {investigation?.generation || 0}</p><code className="run-id">{runId}</code></div>
          <ol className="steps">{(investigation?.steps || []).map(step => <li key={step.id}><span className={`step-dot ${step.status}`} /><span>{roleNames[step.role] || step.role}</span><small>批次 {step.generation} · {statusNames[step.status]}</small></li>)}</ol>
          <details className="evidence"><summary>运行时间线（最近 500 条） · {streamStatus}</summary>
            {timeline.key === streamKey && <ol>{timeline.events.map(item => <li key={item.eventId}><time dateTime={item.occurredAt}>{new Date(item.occurredAt).toLocaleString("zh-CN")}</time> · {item.sequenceNo} · {item.eventType}<p>{item.redactedSummary}</p></li>)}</ol>}
          </details>
          {usage?.key === streamKey && <details className="evidence"><summary>运行用量与步骤耗时</summary>
            <p>费用（微单位）：{usage.data.costMicros === null ? "未知，不能视为零" : usage.data.costMicros} · {usage.data.usageState}</p>
            <p>已记录输入 {usage.data.usage.input_tokens} / 输出 {usage.data.usage.output_tokens} tokens；包含失败代次。</p>
            <ul>{usage.data.attempts.map(item => <li key={item.invocationId}>{roleNames[item.role] || item.role} · {item.status} · {item.elapsedSeconds === null ? "尚未结束" : `${item.elapsedSeconds.toFixed(2)} 秒`}</li>)}</ul>
          </details>}
          {investigation?.mode === "replay" && <p className="replay-note">Replay 演示结果：运行与证据链已执行；业务结论尚未经过真实模型判断。</p>}
          {investigation?.result?.claims.map((claim, index) => <article className="claim" key={index}><p className="eyebrow">回答 {index + 1} · {claim.quality === "supported" ? "有来源引用" : "需要审阅"}</p><p className="conclusion">{claim.conclusion}</p>{claim.unknowns.length > 0 && <div className="uncertainty"><strong>待确认</strong><ul>{claim.unknowns.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}{claim.conflicts.length > 0 && <div className="uncertainty"><strong>来源冲突</strong><ul>{claim.conflicts.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}<details><summary>查看引用 ({claim.citations.length})</summary>{claim.citations.map((citation, i) => <p className="citation" key={i}>观测 {citation.sourceId}<br />SHA-256 {citation.fragmentHash}</p>)}</details></article>)}
          {retainedIssues.length > 0 && <div className="uncertainty"><strong>其他待确认事项</strong><ul>{retainedIssues.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}
          {investigation?.issues.map((issue, i) => <p className="error" key={i}>待处理：{issue}</p>)}
          {investigation?.errorCode && <p className="error">调查未完成：{investigation.errorCode}。已有执行记录已保留。</p>}
          {investigation && <details className="evidence"><summary>证据记录 ({investigation.evidence.length})</summary>{investigation.evidence.map(item => <div className="evidence-row" id={`evidence-${item.id}`} key={item.id}><span>{item.tool_name}</span><time dateTime={item.observed_at}>{new Date(item.observed_at).toLocaleString("zh-CN")}</time><code>{item.id}</code><pre className="evidence-excerpt">{item.excerpt}</pre></div>)}<p className="muted">{investigation.review}。观测记录保留当时输入，不代表来源始终有效。</p></details>}
        </div>}
      </section>
    </main>
  </>;
}
