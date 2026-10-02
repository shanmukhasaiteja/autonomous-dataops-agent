import json

from dataops_agent.agent import AuditLog, DataOpsAgent
from dataops_agent.approvals import AutoApprove, AutoDeny, PolicyApprover
from dataops_agent.environment import INCIDENTS, build_environment
from dataops_agent.llm import HeuristicPlanner, LLMResponse, ToolCall
from dataops_agent.tools import build_registry


def run(approver, incidents=INCIDENTS, llm=None, max_steps=40, audit=None):
    env = build_environment(incidents)
    agent = DataOpsAgent(llm or HeuristicPlanner(), build_registry(env), approver, max_steps, audit)
    return env, agent.run()


def states(env):
    return {name: p.status for name, p in env.pipelines.items()}


def test_policy_mode_fixes_safe_incidents_and_escalates_the_rest():
    env, report = run(PolicyApprover())
    assert report.outcome == "completed"
    assert states(env) == {
        "orders_ingest": "healthy", "clickstream_agg": "healthy", "inventory_sync": "failed",
        "users_dim": "degraded", "billing_export": "healthy",
    }
    assert {e["pipeline"] for e in env.escalations} == {"inventory_sync", "users_dim"}
    assert len(env.notifications) == 1


def test_auto_approve_mode_also_fixes_the_null_spike():
    env, _ = run(AutoApprove())
    assert env.pipelines["users_dim"].status == "healthy"
    assert [e["pipeline"] for e in env.escalations] == ["inventory_sync"]


def test_deny_mode_never_changes_anything_risky_and_escalates_instead():
    env, _ = run(AutoDeny())
    assert env.pipelines["clickstream_agg"].memory_mb == 2048
    assert env.pipelines["users_dim"].version == "v42"
    assert {e["pipeline"] for e in env.escalations} == {"orders_ingest", "clickstream_agg", "inventory_sync", "users_dim"}


def test_healthy_platform_is_left_alone():
    env, report = run(PolicyApprover(), incidents=[])
    assert report.outcome == "completed" and report.steps == 1
    assert env.escalations == [] and env.notifications == []


def test_every_high_risk_call_is_audited_with_a_decision():
    audit = AuditLog()
    run(PolicyApprover(), audit=audit)
    approvals = [e for e in audit.entries if e["kind"] == "approval"]
    assert {e["tool"] for e in approvals} == {"apply_schema_mapping", "scale_job_memory", "rollback_deployment"}
    assert [e["approved"] for e in approvals].count(False) == 1


def test_audit_log_is_written_as_jsonl(tmp_path):
    path = tmp_path / "logs" / "audit.jsonl"
    run(PolicyApprover(), audit=AuditLog(path))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines and all({"ts", "step", "kind"} <= set(line) for line in lines)


class ScriptedLLM:
    def __init__(self, calls):
        self.calls = list(calls)

    def chat(self, messages, tools):
        if not self.calls:
            return LLMResponse(content="done")
        name, args = self.calls.pop(0)
        return LLMResponse(tool_calls=[ToolCall(f"c{len(messages)}", name, args)])


def test_unknown_tools_and_bad_arguments_are_blocked_not_crashed():
    audit = AuditLog()
    llm = ScriptedLLM([("drop_database", {}), ("get_status", {}), ("get_status", {"pipeline": "orders_ingest"})])
    env, report = run(AutoApprove(), incidents=[], llm=llm, audit=audit)
    assert report.outcome == "completed"
    assert [e["kind"] for e in audit.entries] == ["blocked", "blocked", "observe"]


def test_repeating_the_same_state_change_is_blocked():
    audit = AuditLog()
    llm = ScriptedLLM([("restart_job", {"pipeline": "inventory_sync"})] * 5)
    run(AutoApprove(), incidents=["auth_expired"], llm=llm, audit=audit)
    blocked = [e for e in audit.entries if e["kind"] == "blocked"]
    assert len(blocked) == 3 and "repeated" in blocked[0]["detail"]


def test_runaway_agent_is_stopped_by_the_step_limit():
    llm = ScriptedLLM([("list_pipelines", {})] * 100)
    _, report = run(AutoApprove(), incidents=[], llm=llm, max_steps=5)
    assert report.outcome == "step_limit" and report.steps == 5


def test_policy_caps_memory_and_rejects_odd_types():
    policy = PolicyApprover()
    assert policy.decide("scale_job_memory", {"memory_mb": 8192}).approved
    assert not policy.decide("scale_job_memory", {"memory_mb": 16384}).approved
    assert not policy.decide("apply_schema_mapping", {"target_type": "GEOMETRY"}).approved
    assert not policy.decide("rollback_deployment", {}).approved
