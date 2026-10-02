import pytest

from dataops_agent.environment import ToolError, build_environment
from dataops_agent.tools import HIGH, READ, build_registry


@pytest.fixture
def registry():
    return build_registry(build_environment([]))


def test_every_tool_has_a_valid_openai_spec(registry):
    for spec in registry.specs():
        fn = spec["function"]
        assert spec["type"] == "function" and fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"


def test_state_changing_dangerous_tools_are_marked_high_risk(registry):
    for name in ("scale_job_memory", "apply_schema_mapping", "rollback_deployment"):
        assert registry.get(name).risk == HIGH
    for name in ("list_pipelines", "get_logs", "get_status", "run_quality_check"):
        assert registry.get(name).risk == READ


def test_validation_rejects_bad_arguments(registry):
    tool = registry.get("scale_job_memory")
    with pytest.raises(ToolError, match="missing"):
        tool.validate({"pipeline": "x"})
    with pytest.raises(ToolError, match="unexpected"):
        tool.validate({"pipeline": "x", "memory_mb": 1, "sudo": True})
    with pytest.raises(ToolError, match="type"):
        tool.validate({"pipeline": "x", "memory_mb": "lots"})
    with pytest.raises(ToolError, match="type"):
        tool.validate({"pipeline": "x", "memory_mb": True})
    tool.validate({"pipeline": "x", "memory_mb": 4096})
