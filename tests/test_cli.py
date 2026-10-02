import pytest

from dataops_agent.cli import main


def test_cli_runs_a_single_scenario(capsys):
    assert main(["--scenario", "oom", "--approval", "policy"]) == 0
    out = capsys.readouterr().out
    assert "clickstream_agg" in out and "✅ clickstream_agg" in out


def test_openai_mode_requires_an_api_key(monkeypatch, capsys):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert main(["--llm", "openai"]) == 2
    assert "LLM_API_KEY" in capsys.readouterr().err


def test_unknown_scenario_is_rejected():
    with pytest.raises(SystemExit):
        main(["--scenario", "meteor"])
