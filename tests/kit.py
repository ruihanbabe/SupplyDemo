"""测试共用装配（F09 起）：一套完整 Runtime（环境、事件日志、代理层、模型适配层、执行者循环、派发、活跃段、调度），
模型全部用 ScriptedTransport（顺序执行，脚本顺序即调用顺序），不产生任何真实调用。被 test_runtime、test_context 等使用。
"""
import json  # 构造工具参数

from pydantic import BaseModel  # 子 Agent 输出模型

from infrastructure.config import load_harness_config  # 配置
from models.adapter import ModelAdapter  # 模型适配层
from models.providers import ScriptedTransport, load_models_config, reply  # 脚本传输
from permissions.guard import PermissionGuard  # F11 权限检查
from permissions.tokens import Authority, load_permissions_config  # F11 令牌
from runtime.dispatch import Dispatcher, ExecutorSpec  # 派发
from runtime.loop import ExecutorLoop  # 执行者循环
from runtime.segment import Scheduler, SegmentRunner  # 活跃段与调度
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储
from simenv.api import Environment  # 环境
from simenv.bom import load_boms  # BOM
from simenv.params import load_params  # 参数
from tools.archive import MemoryArchive  # 留档
from tools.kv import MemoryKV  # KV
from tools.proxy import ToolProxy, load_tools_config  # 代理层
from tools.quota import QuotaGuard  # 配额
from tools.servers import control_server, distributor_server, environment_server  # Server

MPN = "SKY13453-385LF"  # 射频开关
ORCH_TOOLS = ["dispatch_task", "end_segment", "env_inventory", "ask_user"]  # 编排者工具


class Note(BaseModel):
    """子 Agent 的输出。"""
    note: str  # 结论


# 功能：F11 测试用签发者 / 权限检查 / 令牌。executors 覆盖或追加角色工具（如测试专用执行者 quoter）。
# 依赖：config/permissions.yaml。完成：令牌缺省为 S1 模式、案子 K7。
def authority(executors=None, **kw) -> Authority:
    cfg = load_permissions_config()  # 权限配置
    cfg["executors"] = {**cfg["executors"], **(executors or {})}  # 追加测试执行者
    return Authority(cfg, **kw)  # 签发者


def guard(executors=None, **kw) -> PermissionGuard:
    """测试用权限检查。"""
    return PermissionGuard(authority(executors, **kw))  # 包一层


def tok(proxy_or_guard, executor, case_id="K7", mode="S1", scope=None):
    """给某执行者签一张根令牌（测试直接调代理层时用）；参数可为代理层、权限检查器或签发者。"""
    g = getattr(proxy_or_guard, "guard", proxy_or_guard)  # 代理层或检查器
    auth = g if hasattr(g, "issue_root") else g.authority  # 签发者
    return auth.issue_root(executor=executor, case_id=case_id, mode=mode,  # 签发
                                  initiated_by="test", scope=scope)  # 发起者与作用域


def call(name, **args):
    """构造一个工具调用（参数为 JSON 字符串，GLM 风格）。"""
    return {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}  # 工具调用


def dispatch(n):
    """编排者一轮里派 n 个任务。"""
    return reply(tool_calls=[call("dispatch_task", executor="quoter", task=f"查报价 {i}", reason="比价")  # 派任务
                             for i in range(1, n + 1)])  # n 个


def end(wait_for="human"):
    """编排者结束本段。"""
    return reply(tool_calls=[call("end_segment", wait_for=wait_for, reason="等审批")])  # 结束本段


def inv(i):
    """编排者查一次库存（参数各不相同，避免触发循环检测）。"""
    return reply(tool_calls=[call("env_inventory", mpns=[MPN], board=f"b{i}")])  # 查库存


# 功能：装配一套 Runtime。script 为模型应答顺序；budgets / switches 可覆盖。
# 依赖：F03/F06/F07/F08/F09 全部组件。完成：返回 (session, segments, scheduler, env, transport)。
def build(script, *, budgets=None, max_active=4, context=None, **switches):
    config = load_harness_config()  # 配置
    sw = {**config["ablation"], "model_fallback": False, **switches}  # 不降级，脚本只走 GLM
    rt = {**config["runtime"], **(budgets or {})}  # 预算
    session = Session(MemorySessionStore(), "run", clock=lambda: "t")  # 事件日志
    env = Environment.create(load_params(), load_boms(), 42)  # 环境
    holder = {}  # 延迟绑定控制处理函数（SegmentRunner 依赖代理层，代理层依赖处理函数）
    kv = MemoryKV()  # KV
    tcfg = load_tools_config()  # 工具配置
    auth = authority({"quoter": ["distributor_quotes"]}, scope_enabled=sw["capability_scope"])  # F11 签发者
    proxy = ToolProxy([environment_server(lambda: env), distributor_server(),  # Server
                       control_server(lambda n, a, c: holder["seg"].handle_control(n, a, c))], session,  # 控制
                      kv=kv, quota=QuotaGuard(kv, tcfg["providers"]), archive=MemoryArchive(), config=tcfg,
                      guard=PermissionGuard(auth), switches=sw)  # F11 权限与开关
    transport = ScriptedTransport(script)  # 脚本
    adapter = ModelAdapter(load_models_config(), {"glm-flash": transport}, session, switches=sw)  # 适配层
    fold = config["context"]["fold_after_rounds"] if sw["tool_result_folding"] else None  # F16 折叠
    loop = ExecutorLoop(adapter, proxy, loop_detection=sw["loop_detection"], fold_after=fold)  # 执行者循环
    registry = {"quoter": ExecutorSpec("quoter", "SourcingNote", system="你查报价。",  # 一个 LLM 子 Agent
                                       tools=["distributor_quotes"], output=Note)}  # 工具与输出
    dispatcher = Dispatcher(session, loop, registry, budgets=rt, authority=auth, batch_join=sw["batch_join"])  # 派发器
    extra = {"context_builder": context} if context is not None else {}  # F16 上下文构建器（可选）
    seg = SegmentRunner(session, loop, dispatcher, budgets=rt, orchestrator_tools=ORCH_TOOLS,  # 活跃段
                        authority=auth, **extra)  # F11 签发者
    holder["seg"] = seg  # 绑定
    sched = Scheduler(env, session, seg, max_active=max_active)  # 调度
    return session, seg, sched, env, transport  # 返回


def open_case(session, case_id="K7", day=0):
    """开一个案子。"""
    session.open_case(case_id, day=day, actor="runtime", payload={"goal": "处置缺口"})  # 开案


def count(session, type, case_id="K7"):
    """某类事件数。"""
    return len(session.events(case_id=case_id, type=type))  # 计数




# 功能：只带分销商 Server 的代码执行链（F13 起）：事件日志、环境、代理层、三个代码执行者、派发器、采购执行器。
# 依赖：F07/F09/F11/F21/F10；gate 为可选的门控工厂 (proxy, claims) → 门控（F12 传 EvidenceGate），缺省为 F21 最小门控；
#       unlimited 去掉配额与限流（令牌桶按真实时间，连续调用会被限流）。
# 完成：返回命名空间 {s, env, proxy, run, ex}；run(执行者, 输入, 参数) 派一个任务、立即汇合，返回 (引用, Artifact)。
def code_pipeline(*, gate=None, sinks=None, overlays=(), switches=None, unlimited=False, claims=None, case_id="K7",
                  providers=None):
    from types import SimpleNamespace  # 命名空间

    from business.components import code_executors  # 代码执行者
    from business.procurement import ProcurementExecutor, minimal_gate  # 执行器与最小门控

    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    env = Environment.create(load_params(), load_boms(), 42)  # 环境
    cfg, kv = load_tools_config(), MemoryKV()  # 工具配置与 KV
    if unlimited:  # 测试不受真实时间令牌桶影响（一次测试里连续几十次调用）
        cfg["providers"] = {k: {"daily_limit": None, "rate_per_s": None, "burst": None} for k in cfg["providers"]}
    cfg["providers"].update(providers or {})  # 覆盖个别提供方（如某家配额为 0，测部分成功）
    proxy = ToolProxy([distributor_server()], s, kv=kv, quota=QuotaGuard(kv, cfg["providers"]),  # 代理层
                      archive=MemoryArchive(), config=cfg, guard=guard(), overlays=overlays,  # 权限与注入
                      switches=switches)  # 开关
    book = claims(s) if claims else None  # F10 软声明（工厂：session → ClaimBook）
    registry = code_executors(lambda: env, proxy, run_id="r", sinks=sinks, claims=book)  # 代码执行者
    disp = Dispatcher(s, loop=None, registry=registry, budgets=load_harness_config()["runtime"],  # 派发器
                      authority=proxy.guard.authority)  # 子令牌
    s.open_case(case_id, day=0, actor="runtime", payload={"mode": "S1"})  # 开案（处置模式）

    def run(executor, inputs=(), params=None, day=0, case=case_id):  # 派一个任务并立即汇合（可指定案子）
        parent = tok(proxy, "orchestrator", case)  # 编排者根令牌
        disp.dispatch(parent=parent, day=day, executor=executor, task=executor, inputs=list(inputs), reason="t",
                      params=params)  # 派发
        disp.run_pending(case_id=case, day=day)  # 运行
        ref = s.events(type="task_finished")[-1]["payload"]["artifact"]  # 产出引用
        art_id, _, ver = ref.rpartition("@v")  # 拆引用
        return ref, s.artifact_view(art_id, int(ver))  # 引用与 Artifact

    ex = ProcurementExecutor(s, lambda: env, gate=gate(proxy, book) if gate else minimal_gate, sinks=sinks)  # 执行器
    return SimpleNamespace(s=s, env=env, proxy=proxy, run=run, ex=ex, claims=book)  # 返回
