import pytest

from dataops_agent.environment import MAX_MEMORY_MB, ToolError, build_environment


def test_restart_alone_does_not_fix_an_oom_job():
    env = build_environment(["oom"])
    assert env.restart_job("clickstream_agg")["status"] == "failed"


def test_scaling_memory_then_restarting_fixes_oom():
    env = build_environment(["oom"])
    env.scale_job_memory("clickstream_agg", 4096)
    assert env.restart_job("clickstream_agg")["status"] == "healthy"


def test_memory_cap_is_enforced():
    env = build_environment(["oom"])
    with pytest.raises(ToolError):
        env.scale_job_memory("clickstream_agg", MAX_MEMORY_MB + 1)


def test_schema_drift_needs_a_mapping_before_restart_succeeds():
    env = build_environment(["schema_drift"])
    assert env.restart_job("orders_ingest")["status"] == "failed"
    diff = env.get_schema_diff("orders_ingest")["differences"]
    assert diff == [{"column": "amount", "expected": "DOUBLE", "actual": "STRING"}]
    env.apply_schema_mapping("orders_ingest", "amount", "DOUBLE")
    assert env.get_schema_diff("orders_ingest")["differences"] == []
    assert env.restart_job("orders_ingest")["status"] == "healthy"


def test_expired_credentials_cannot_be_fixed_by_restarting():
    env = build_environment(["auth_expired"])
    for _ in range(3):
        assert env.restart_job("inventory_sync")["status"] == "failed"


def test_rollback_fixes_a_null_spike_and_quality_check_confirms():
    env = build_environment(["null_spike"])
    assert not env.run_quality_check("users_dim")["passed"]
    env.rollback_deployment("users_dim", "bad deploy")
    assert env.run_quality_check("users_dim")["passed"]


def test_unknown_pipeline_and_unknown_incident_are_rejected():
    env = build_environment([])
    with pytest.raises(ToolError):
        env.get_status("nope")
    with pytest.raises(ValueError):
        build_environment(["meteor_strike"])
