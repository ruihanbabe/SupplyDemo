"""Print what a caller may actually reach, and where each answer came from.

The point is the last column. A tool that is missing from a model's toolbox is
indistinguishable, from the outside, from one that failed — and when it is missing by
mistake, "denied" alone does not say which of three files to open.

Run with: make explain WORKER=spec_check
"""
from __future__ import annotations

import argparse
import sys

from permissions.policy import SessionPolicy, load_policy
from tools.catalog_tools import build_registry

MARK = {True: "✓ allow", False: "✗ deny "}


def explain(worker: str, *, scenario: str | None = "procurement",
            mode: str = "ask", withheld: frozenset[str] = frozenset()) -> list[dict]:
    registry = build_registry()
    policy = load_policy(worker, session=SessionPolicy(mode=mode, withheld=withheld))
    rows = []
    for name in sorted(registry.names()):
        tool = registry.describe(name)
        in_scenario = not tool.scenarios or scenario in tool.scenarios
        if not in_scenario:
            rows.append({"tool": name, "permission": tool.permission, "allowed": False,
                         "layer": "scenario", "reason": f"不属于场景 {scenario}"})
            continue
        decision = policy.evaluate(tool.permission, effect=tool.effect)
        reason = decision.reason
        if decision.allowed and not tool.available:
            # Allowed by policy, still unusable. Reported separately so a reader does not
            # go looking for a permission problem that is not there.
            rows.append({"tool": name, "permission": tool.permission, "allowed": False,
                         "layer": "tool", "reason": tool.unavailable_reason})
            continue
        rows.append({"tool": name, "permission": tool.permission,
                     "allowed": decision.allowed, "layer": decision.layer,
                     "reason": reason})
    return rows


def render(rows: list[dict]) -> str:
    width = max((len(row["tool"]) for row in rows), default=4)
    permission_width = max((len(row["permission"]) for row in rows), default=10)
    lines = [f"{'工具'.ljust(width)}  {'权限'.ljust(permission_width)}  判定      来源      理由"]
    for row in rows:
        lines.append(f"{row['tool'].ljust(width)}  {row['permission'].ljust(permission_width)}  "
                     f"{MARK[row['allowed']]}  {(row['layer'] or '-').ljust(8)}  {row['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解释某个 worker 在某个会话中实际能调用什么")
    parser.add_argument("--worker", required=True)
    parser.add_argument("--scenario", default="procurement")
    parser.add_argument("--mode", default="ask", choices=("explore", "ask", "auto"))
    parser.add_argument("--withhold", action="append", default=[],
                        help="本会话额外撤回的权限，可重复")
    args = parser.parse_args(argv)
    print(render(explain(args.worker, scenario=args.scenario, mode=args.mode,
                         withheld=frozenset(args.withhold))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
