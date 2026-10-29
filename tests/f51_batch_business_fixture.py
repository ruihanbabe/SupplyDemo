"""Expand each business fixture's first assistant call to three durable calls."""

from copy import deepcopy


class BatchBusinessFixture:
    def __init__(self, base, glm, repeat_all=False):
        self.base, self.glm = base, glm
        self.repeat_all = repeat_all

    async def stream(self, policy, payload):
        events = [event async for event in self.base.stream(policy, payload)]
        tool = payload["tools"][0]["function"]["name"] if self.glm else payload["tools"][0]["name"]
        # Repeat the bounded supply read; large CRM/commercial payloads retain one call.
        if tool != "supply_bound_impact" and not self.repeat_all:
            for event in events:
                yield event
            return
        if self.glm:
            for event in events:
                result = deepcopy(event)
                delta = result["choices"][0]["delta"]
                if "tool_calls" in delta:
                    original = delta["tool_calls"][0]
                    delta["tool_calls"] = []
                    for i in range(3):
                        call = deepcopy(original)
                        call["index"] = i
                        if "id" in call:
                            call["id"] += f"_{i}"
                        delta["tool_calls"].append(call)
                yield result
        elif any(
            e["type"] == "response.output_item.added" and e["item"]["type"] == "function_call"
            for e in events
        ):
            output = []
            for i in range(3):
                for event in events:
                    if event["type"] == "response.completed":
                        continue
                    result = deepcopy(event)
                    if "output_index" in result:
                        result["output_index"] = i
                    if "item_id" in result:
                        result["item_id"] += f"_{i}"
                    if "item" in result:
                        result["item"]["id"] += f"_{i}"
                        result["item"]["call_id"] += f"_{i}"
                        if result["type"] == "response.output_item.done":
                            output.append(result["item"])
                    yield result
            terminal = deepcopy(events[-1])
            terminal["response"]["output"] = output
            yield terminal
        else:
            for event in events:
                yield event
