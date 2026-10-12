"""文件摘要（F22 · 整套 Runtime 装配）

build_runtime(...) 在一处把 F03–F22 的组件接好，消融开关一律取 config/harness.yaml（可覆盖），返回 Runtime：
- 工具层：环境（含合并查询 env_lookup）、分销商、留档、编排控制、采购（含合并写 po_action）、datasheet（术语表同义词 + 板级文档）、
  知识库 B 七个 Server；F11 权限、F15 重试熔断、F18 注入计划（可选）、F13 疑似指令检测随代理层生效。
- 模型：F08 适配层（限流复用配额组件、重试熔断、注入包装）；F09 执行者循环（循环检测、F16 折叠）。
- 业务与门控：F21 代码节点（带 F10 软声明）+ F23 五个 LLM 节点（原料画像、替代检索、Verifier、Resolver、Explainer）+ 调用方追加的节点；F12 证据门控（审批令牌、软声明、datasheet 刷新）；
  F15/F18 采购执行器（两步写、注入挂点）；HumanDesk、GoalDesk、PlannerDesk、BuyerDesk 为人工入口；F05 模拟人工（可选）挂在调度的路由前。
- 编排：F22 按模式的节点集合与工具集、Planner（计划校验与模板兜底）；F16 上下文（记忆分区）；F14 升级；F20 知识库。
- 调度：F10 CaseRouter（F14 反馈循环、F20 知识库同步）接 F09 Scheduler。
Runtime.start()：进程启动时补齐两步写（F15）并写运行清单（F18）；run_day() 推进一天；template_driver() 给固定模板对照组。
依赖：上述各包。被 Eval 运行器（F27）、演示（F29）与测试使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import time  # 默认睡眠
from dataclasses import dataclass  # 结果

from agents.functions import load_agents_config  # F23 配置
from agents.nodes import llm_executors  # F23 LLM 节点
from business.components import code_executors  # 代码节点
from business.procurement import ProcurementExecutor, procurement_server  # 采购
from cases.claims import ClaimBook  # 软声明
from cases.router import CaseRouter  # 路由
from context.assemble import OrchestratorContext  # 上下文
from datasheets.fetch import FetchRejected, Ingestor  # datasheet 入库
from datasheets.parse import FixtureParser  # 样例解析
from datasheets.server import datasheet_server  # 文档工具
from datasheets.store import MemoryDatasheetStore  # 页表
from feedback.escalation import Escalation  # 升级
from feedback.loop import FeedbackLoop  # 反馈循环
from gate.approval import ApprovalVault, HumanDesk  # 审批
from gate.evidence import EvidenceGate  # 证据门控
from humans.desks import BuyerDesk, PlannerDesk, load_humans_config  # F05 新人工入口
from humans.sim import SimulatedHumans  # F05 模拟人工
from infrastructure.config import load_harness_config  # 配置
from knowledge_base.assets import CompositeParser, CompositeSource, Glossary  # 知识资产
from knowledge_base.kb import KnowledgeBase, MemoryKBStore, case_memory, kb_server  # 知识库 B
from models.adapter import ModelAdapter  # 模型适配层
from models.providers import load_models_config  # 模型配置
from orchestrator.planning import GoalDesk, Planner, load_orchestrator_config  # 计划与目标
from orchestrator.template import TemplateDriver  # 固定模板
from permissions.guard import PermissionGuard  # 权限
from permissions.tokens import Authority, load_permissions_config  # 令牌
from replay.faults import FaultPlan  # 注入
from replay.manifest import build_manifest, record_manifest  # 运行清单
from resilience.policy import Resilience  # 重试熔断
from runtime.dispatch import Dispatcher  # 派发
from runtime.loop import ExecutorLoop  # 执行者循环
from runtime.segment import Scheduler, SegmentRunner  # 活跃段与调度
from session.log import Session  # 事件日志
from simenv.api import Environment  # 环境
from tools.archive import MemoryArchive  # 留档
from tools.kv import MemoryKV  # KV
from tools.proxy import ToolProxy, load_tools_config  # 代理层
from tools.quota import QuotaGuard  # 配额
from tools.servers import (  # Server
    archive_server,
    control_server,
    distributor_server,
    environment_server,
)


class NoDatasheetSource:
    """没有配置 datasheet 来源时：明确报"尚未具备"（真实下载需用户授权，G5）。"""

    def get(self, key: str):
        raise FetchRejected("datasheet 来源尚未配置（真实下载需用户授权；测试请传 FixtureSource）")  # 不静默成功


@dataclass
class Runtime:
    """装配好的整套 Runtime（字段即各组件，便于测试与 Eval 直接取用）。"""
    session: Session  # 事件日志
    env: Environment  # 环境
    switches: dict  # 生效的消融开关
    authority: Authority  # 令牌签发者
    proxy: ToolProxy  # 工具代理层
    adapter: ModelAdapter  # 模型适配层
    dispatcher: Dispatcher  # 派发器
    segments: SegmentRunner  # 活跃段
    scheduler: Scheduler  # 调度
    router: CaseRouter  # 案子路由
    executor: ProcurementExecutor  # 采购执行器
    desk: HumanDesk  # 人工入口（审批、H1）
    goals: GoalDesk  # 目标修订（H4）
    knowledge: KnowledgeBase  # 知识库 B
    ingestor: Ingestor  # datasheet 入库
    fault_plan: FaultPlan | None  # 注入计划
    seeds: dict  # 种子（写进运行清单）
    planner_desk: PlannerDesk | None = None  # F05 计划员入口（H5）
    buyer_desk: BuyerDesk | None = None  # F05 采购员消歧入口（H3）
    humans: SimulatedHumans | None = None  # F05 模拟人工（None = 不自动应答）

    def start(self) -> dict:
        """进程启动：补齐两步写（F15）→ 写运行清单（F18）。"""
        recovered = self.executor.recover_pending_writes(day=self.env.state.day)  # 两步写恢复
        manifest = build_manifest(seeds=self.seeds, models_config=load_models_config(),  # 运行清单
                                  archive_source=getattr(self.proxy.replay, "run_id", None))  # 留档源
        record_manifest(self.session, manifest, day=self.env.state.day)  # 事件
        return {"recovered": recovered, "manifest": manifest}  # 结果

    def run_day(self) -> dict:
        """推进一天（F09 调度 + F10 路由）。"""
        return self.scheduler.run_day()  # 一天

    def template_driver(self) -> TemplateDriver:
        """固定模板对照组驱动器。"""
        return TemplateDriver(self.session, self.dispatcher, self.executor, self.authority)  # 驱动器


# 功能：见文件摘要。依赖：各组件构造函数。
# 完成：transports 为 {端点名: 传输}；tools_config / switches 可覆盖（测试去掉真实时间限流）；extra_executors 为 LLM 节点注册项（F23）；
#       humans 为 F05 模拟人工规则（True = config/humans.yaml，dict = 指定；不传则不自动应答），种子取 seeds["humans"]，缺省用 seeds["env"]。
def build_runtime(*, env: Environment, session: Session, transports: dict, kv=None, archive=None,
                  kb_store=None, datasheet_store=None, datasheet_source=None, faults: list[dict] | None = None,
                  switches: dict | None = None, tools_config: dict | None = None, extra_executors: dict | None = None,
                  seeds: dict | None = None, sleep=time.sleep, vault_key: bytes | None = None,
                  humans: dict | bool | None = None) -> Runtime:
    config = load_harness_config()  # Harness 配置
    sw = {**config["ablation"], **(switches or {})}  # 消融开关
    rt = config["runtime"]  # 预算
    ocfg = load_orchestrator_config()  # 编排者配置
    kv = kv or MemoryKV()  # 缓存 / 配额 / 熔断
    tcfg = tools_config or load_tools_config()  # 工具配置
    quota = QuotaGuard(kv, tcfg["providers"])  # 配额（模型端点复用）
    auth = Authority(load_permissions_config(), scope_enabled=sw["capability_scope"])  # F11
    res = Resilience(kv, sleep=sleep, retry=sw["auto_retry"], breaker=sw["circuit_breaker"])  # F15
    plan = FaultPlan(session, faults) if faults else None  # F18
    claims = ClaimBook(session, enabled=sw["supply_claims"])  # F10
    vault = ApprovalVault(key=vault_key)  # F12
    glossary = Glossary(enabled=sw["glossary_injection"])  # F20 术语表
    ds_store = datasheet_store or MemoryDatasheetStore()  # F19 页表
    ingestor = Ingestor(CompositeSource(datasheet_source or NoDatasheetSource()),  # datasheet + 板级文档
                        CompositeParser(FixtureParser()), ds_store, session)  # 解析
    kb = KnowledgeBase(kb_store or MemoryKBStore(), inject_enabled=sw["memory_injection"])  # F20 知识库 B
    holder: dict = {}  # 延迟绑定（控制工具 → 活跃段；采购工具 → 执行器）

    def get_env() -> Environment:  # 当前环境
        return env  # 固定对象

    proxy = ToolProxy([environment_server(get_env), distributor_server(), archive_server(),  # Server
                       control_server(lambda n, a, c: holder["seg"].handle_control(n, a, c)),  # 编排控制
                       procurement_server(lambda: holder["ex"]),  # 采购
                       datasheet_server(ds_store, ingestor, use_synonyms=sw["datasheet_synonyms"],  # 文档
                                        synonyms=glossary.synonyms()),  # 术语表同义词
                       kb_server(kb)], session,  # 知识库 B
                      kv=kv, quota=quota, archive=archive or MemoryArchive(), config=tcfg,  # 缓存、配额、留档
                      guard=PermissionGuard(auth), switches=sw, resilience=res,  # 权限、开关、重试
                      faults=[plan.tool_fault] if plan else ())  # 注入
    wrapped = {n: plan.wrap_transport(n, t) for n, t in transports.items()} if plan else transports  # 模型注入
    adapter = ModelAdapter(load_models_config(), wrapped, session, limiter=quota, switches=sw, resilience=res)  # F08
    fold = config["context"]["fold_after_rounds"] if sw["tool_result_folding"] else None  # F16 折叠
    loop = ExecutorLoop(adapter, proxy, loop_detection=sw["loop_detection"], fold_after=fold)  # F09
    gate = EvidenceGate(vault, proxy=proxy, authority=auth, revalidate=sw["presubmit_revalidation"],  # F12
                        claims=claims, datasheet_refresh=lambda mpn, day: ingestor.refresh(mpn, day=day))  # 声明与刷新
    executor = ProcurementExecutor(session, get_env, gate=gate, on_point=plan.kill_point if plan else None)  # 采购
    holder["ex"] = executor  # 绑定
    llm_nodes = llm_executors(store=ds_store, kb=kb, glossary=glossary, adapter=adapter, get_env=get_env,  # F23 五个节点
                              switches=sw, agents_config=load_agents_config(), session=session)  # 开关与配置
    registry = {**code_executors(get_env, proxy, run_id=session.run_id, claims=claims), **llm_nodes,  # 代码 + LLM 节点
                **(extra_executors or {})}  # 调用方追加
    node_sets = {m: set(c["nodes"]) for m, c in ocfg["modes"].items()}  # 模式节点集合
    dispatcher = Dispatcher(session, loop, registry, budgets=rt, authority=auth, batch_join=sw["batch_join"],
                            node_sets=node_sets)  # 派发
    planner = Planner(session, registry=set(registry), max_llm_calls=rt["case"]["max_llm_calls"], config=ocfg,
                      validate=sw["plan_validation"])  # 计划校验
    goals = GoalDesk(session)  # 目标修订
    context = OrchestratorContext(config["context"], enabled=sw["context_assembly"], memory=case_memory(kb))  # F16
    seg = SegmentRunner(session, loop, dispatcher, budgets=rt, orchestrator_tools=None, authority=auth,  # 活跃段
                        escalation=Escalation(session), knowledge=kb, planner=planner, goals=goals,  # 升级、知识、计划
                        tools_by_mode={m: c["tools"] for m, c in ocfg["modes"].items()}, context_builder=context)
    holder["seg"] = seg  # 绑定
    feedback = FeedbackLoop(session, get_env, cards=sw["feedback_cards"], deviation=sw["deviation_feedback"],
                            escalation=sw["forced_escalation"])  # F14
    router = CaseRouter(session, get_env, merge=sw["same_source_merge"], feedback=feedback, knowledge=kb)  # F10
    desk = HumanDesk(session, vault)  # 人工入口（H1、H2）
    hcfg = load_humans_config() if humans is True else humans  # F05：True = 默认规则，dict = 指定规则
    planner_desk = PlannerDesk(session, get_env, hcfg or load_humans_config())  # H5 入口
    buyer_desk = BuyerDesk(session)  # H3 消歧入口
    sim = (SimulatedHumans(session, desk=desk, goals=goals, knowledge=kb, planner=planner_desk, buyer=buyer_desk,
                           config=hcfg, seed=(seeds or {}).get("humans", (seeds or {}).get("env", 0)))
           if hcfg else None)  # 不传则不自动应答
    scheduler = Scheduler(env, session, seg, router=router, max_active=rt["max_active_cases"],  # F09
                          before_routing=sim.respond if sim else None)  # F05 路由前应答
    return Runtime(session, env, sw, auth, proxy, adapter, dispatcher, seg, scheduler, router, executor,  # 结果
                   desk, goals, kb, ingestor, plan, dict(seeds or {}), planner_desk, buyer_desk, sim)  # 人工入口等

