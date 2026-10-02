"""Human-in-the-loop approval policies for high-risk tool calls."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str


class Approver(Protocol):
    def decide(self, tool: str, args: dict) -> Decision: ...


class AutoApprove:
    def decide(self, tool: str, args: dict) -> Decision:
        return Decision(True, "auto-approved (demo mode)")


class AutoDeny:
    def decide(self, tool: str, args: dict) -> Decision:
        return Decision(False, "all high-risk actions are denied in this mode")


class PolicyApprover:
    """Approves only changes that are bounded and cheap to undo; everything else needs a human."""

    MAX_MEMORY_MB = 8_192
    SAFE_TYPES = frozenset({"DOUBLE", "BIGINT", "INT", "STRING", "TIMESTAMP", "DATE", "BOOLEAN"})

    def decide(self, tool: str, args: dict) -> Decision:
        if tool == "scale_job_memory":
            if args.get("memory_mb", 0) <= self.MAX_MEMORY_MB:
                return Decision(True, f"memory within the {self.MAX_MEMORY_MB} MB policy cap")
            return Decision(False, f"exceeds the {self.MAX_MEMORY_MB} MB policy cap")
        if tool == "apply_schema_mapping":
            if str(args.get("target_type", "")).upper() in self.SAFE_TYPES:
                return Decision(True, "type cast to a standard type")
            return Decision(False, "unrecognised target type")
        return Decision(False, f"'{tool}' always requires human approval")


class InteractiveApprover:
    def decide(self, tool: str, args: dict) -> Decision:
        if not sys.stdin.isatty():
            return Decision(False, "no interactive terminal available")
        answer = input(f"  ⚠️  Approve {tool}({args})? [y/N] ").strip().lower()
        return Decision(answer in {"y", "yes"}, "approved by operator" if answer in {"y", "yes"} else "denied by operator")


APPROVERS = {"policy": PolicyApprover, "auto": AutoApprove, "deny": AutoDeny, "ask": InteractiveApprover}
