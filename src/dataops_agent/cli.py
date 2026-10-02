from __future__ import annotations

import argparse
import sys

from .agent import AuditLog, DataOpsAgent
from .approvals import APPROVERS
from .environment import INCIDENTS, build_environment
from .llm import HeuristicPlanner, LLMError, OpenAICompatibleClient
from .tools import build_registry

ICONS = {"observe": "🔍", "action": "🔧", "approval": "🛡️ ", "blocked": "⛔", "error": "❗"}


def _print_event(event: dict) -> None:
    kind = event["kind"]
    args = ", ".join(f"{k}={v}" for k, v in (event.get("args") or {}).items())
    line = f"{ICONS.get(kind, '•')} [{event['step']:>2}] {event.get('tool', '')}({args})"
    if kind == "approval":
        verdict = "APPROVED" if event["approved"] else "DENIED"
        line += f"  →  {verdict}: {event['detail']}"
    elif kind in {"blocked", "error"}:
        line += f"  →  {event['detail']}"
    print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dataops-agent", description="Autonomous DataOps incident agent")
    parser.add_argument("--scenario", default="all", choices=["all", *INCIDENTS], help="which incident(s) to inject")
    parser.add_argument("--llm", default="heuristic", choices=["heuristic", "openai"],
                        help="heuristic = offline rule-based planner, openai = real LLM via an OpenAI-compatible API")
    parser.add_argument("--approval", default="policy", choices=sorted(APPROVERS), help="how high-risk actions are approved")
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--audit-log", default=None, help="write a JSONL audit trail to this path")
    args = parser.parse_args(argv)

    env = build_environment(INCIDENTS if args.scenario == "all" else [args.scenario])
    try:
        llm = OpenAICompatibleClient() if args.llm == "openai" else HeuristicPlanner()
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    agent = DataOpsAgent(llm, build_registry(env), APPROVERS[args.approval](), args.max_steps,
                         AuditLog(args.audit_log), _print_event)
    broken = [p.name for p in env.pipelines.values() if p.status != "healthy"]
    print(f"🚨 {len(broken)} unhealthy pipeline(s): {', '.join(broken) or 'none'}")
    print(f"   llm={args.llm}  approval={args.approval}\n")
    report = agent.run()

    print(f"\n📋 {report.summary}  ({report.steps} steps, outcome: {report.outcome})\n")
    print("Final pipeline state:")
    for p in env.pipelines.values():
        print(f"  {'✅' if p.status == 'healthy' else '❌'} {p.name:<16} {p.status}")
    for esc in env.escalations:
        print(f"  🎫 {esc['ticket']} → {esc['pipeline']}: {esc['summary']}")
    if args.audit_log:
        print(f"\nAudit trail: {args.audit_log}")
    return 0 if report.outcome == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
