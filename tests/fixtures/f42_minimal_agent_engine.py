"""F42 P3 的第二个最小引擎：只实现 SupplyAgent JSONL，不导入 Pi 或 Node。"""

from __future__ import annotations

import json
import sys


def send(payload: dict[str, object]) -> None:
    print(json.dumps(payload), flush=True)


start = json.loads(sys.stdin.readline())
turn_id = start["turnId"]
send({"protocolVersion": 1, "type": "turn.started", "turnId": turn_id})
send(
    {
        "protocolVersion": 1,
        "type": "tool.call",
        "turnId": turn_id,
        "callId": "fixture-1",
        "name": "supply.impact",
        "arguments": {"partNumber": "PART-A"},
    }
)
json.loads(sys.stdin.readline())
send({"protocolVersion": 1, "type": "model.delta", "turnId": turn_id, "delta": "Fixture complete."})
send({"protocolVersion": 1, "type": "turn.completed", "turnId": turn_id, "status": "completed"})
