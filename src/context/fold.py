"""文件摘要（F16 · 活跃段内旧工具结果折叠）

活跃段内 N 轮之前的原始工具结果折叠为一行存根，附留档记录号（spec §5.9）：
"[已折叠] distributor_quotes：成功，3 条，全文用 read_record(cursor='rec-95')"。
依赖：无（只解析 F07 统一返回格式的 JSON）。被 runtime.loop.ExecutorLoop 在每次调模型前调用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 解析工具结果

FOLDED = "[已折叠] "  # 存根前缀（已折叠的不再处理）


# 功能：把一条工具结果折叠为存根。依赖：F07 统一返回格式。完成：非 JSON 或无记录号时也给出可读存根。
def stub(tool: str, content: str) -> str:
    try:  # 解析统一返回
        result = json.loads(content)  # 字典
    except json.JSONDecodeError:  # 不是统一返回（如"已查过"反馈）
        return FOLDED + f"{tool}：{content[:40]}……"  # 截短
    rid = (result.get("source") or {}).get("record_id")  # 留档号
    data = result.get("data")  # 数据
    size = f"{len(data)} 条" if isinstance(data, list) else "1 项"  # 规模
    status = "成功" if result.get("ok") else f"失败（{(result.get('error') or {}).get('type')}）"  # 结论
    tail = f"，全文用 read_record(cursor='{rid}')" if rid else ""  # 读全文的方式
    return FOLDED + f"{tool}：{status}，{size}{tail}"  # 存根


# 功能：就地折叠 messages 中过旧的工具结果。依赖：stub。
# 完成：rounds[i] 为第 i 条消息所属轮次；tools[i] 为工具名；round_ - k >= after 的工具消息折叠；返回折叠条数。
def fold_old_results(messages: list, rounds: list[int], tools: list[str | None], round_: int, after: int) -> int:
    folded = 0  # 计数
    for i, m in enumerate(messages):  # 逐条
        if m.role == "tool" and not m.content.startswith(FOLDED) and round_ - rounds[i] >= after:  # 够旧
            m.content = stub(tools[i] or "tool", m.content)  # 折叠
            folded += 1  # 计数
    return folded  # 返回
