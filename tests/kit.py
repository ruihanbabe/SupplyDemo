"""测试共用装配（F09 起）：一套完整 Runtime（环境、事件日志、代理层、模型适配层、执行者循环、派发、活跃段、调度），
模型全部用 ScriptedTransport（顺序执行，脚本顺序即调用顺序），不产生任何真实调用。被 test_runtime、test_context 等使用。
"""
import json  # 构造工具参数

from pydantic import BaseModel  # 子 Agent 输出模型

from infrastructure.config import load_harness_config  # 配置
from models.adapter import ModelAdapter  # 模型适配层
from models.providers import ScriptedTransport, load_models_config, reply  # 脚本传输
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
    proxy = ToolProxy([environment_server(lambda: env), distributor_server(),  # Server
                       control_server(lambda n, a, c: holder["seg"].handle_control(n, a, c))], session,  # 控制
                      kv=kv, quota=QuotaGuard(kv, tcfg["providers"]), archive=MemoryArchive(), config=tcfg,
                      whitelist={"orchestrator": set(ORCH_TOOLS), "quoter": {"distributor_quotes"}},  # 白名单
                      switches=sw)  # 开关
    transport = ScriptedTransport(script)  # 脚本
    adapter = ModelAdapter(load_models_config(), {"glm-flash": transport}, session, switches=sw)  # 适配层
    fold = config["context"]["fold_after_rounds"] if sw["tool_result_folding"] else None  # F16 折叠
    loop = ExecutorLoop(adapter, proxy, loop_detection=sw["loop_detection"], fold_after=fold)  # 执行者循环
    registry = {"quoter": ExecutorSpec("quoter", "SourcingNote", system="你查报价。",  # 一个 LLM 子 Agent
                                       tools=["distributor_quotes"], output=Note)}  # 工具与输出
    dispatcher = Dispatcher(session, loop, registry, budgets=rt, batch_join=sw["batch_join"])  # 派发器
    extra = {"context_builder": context} if context is not None else {}  # F16 上下文构建器（可选）
    seg = SegmentRunner(session, loop, dispatcher, budgets=rt, orchestrator_tools=ORCH_TOOLS, **extra)  # 活跃段
    holder["seg"] = seg  # 绑定
    sched = Scheduler(env, session, seg, max_active=max_active)  # 调度
    return session, seg, sched, env, transport  # 返回


def open_case(session, case_id="K7", day=0):
    """开一个案子。"""
    session.open_case(case_id, day=day, actor="runtime", payload={"goal": "处置缺口"})  # 开案


def count(session, type, case_id="K7"):
    """某类事件数。"""
    return len(session.events(case_id=case_id, type=type))  # 计数


