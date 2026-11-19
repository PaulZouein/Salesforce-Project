from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_server import server

from .conftest import SIMULATED_TOKEN

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "mcp__sf-investigator__"

EXPECTED_WRITE_TOOLS = {
    "set_trace_flag", "execute_anonymous_apex", "update_records", "deploy_component", "rollback_change",
    "run_apex_tests_on_production",
}


def generate_settings():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import generate_settings as module
    finally:
        sys.path.pop(0)
    return module


def server_env(sim, project) -> dict[str, str]:
    return {**os.environ, "SFTI_SF_EXECUTABLE": str(sim.launcher), "SFTI_PROJECT_ROOT": str(project.root),
            "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"}


def over_stdio(sim, project, calls):
    """Start the server as Claude Code does (``python -m mcp_server``) and run ``calls`` against it."""

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server"],
                                       env=server_env(sim, project), cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                results = []
                for name, arguments in calls:
                    result = await session.call_tool(name, arguments)
                    results.append(json.loads(result.content[0].text))
                return tools.tools, results

    return asyncio.run(asyncio.wait_for(run(), timeout=120))


# --- tool lists and settings ------------------------------------------------------------


def test_write_tools_are_exactly_the_expected_ones():
    assert set(server.WRITE_TOOLS) == EXPECTED_WRITE_TOOLS
    assert not set(server.WRITE_TOOLS) & set(server.READ_ONLY_TOOLS)
    assert set(server.TOOLS) == set(server.WRITE_TOOLS) | set(server.READ_ONLY_TOOLS)
    assert len(server.TOOLS) == len(server.WRITE_TOOLS) + len(server.READ_ONLY_TOOLS)


def test_write_tools_say_so_in_their_description():
    for name in server.WRITE_TOOLS:
        doc = server.TOOLS[name].__doc__ or ""
        assert doc.lstrip().startswith("WRITE.") or "PRODUCTION" in doc, name


@pytest.mark.parametrize("guard", [True, False])
def test_generated_settings_ask_for_every_write_and_allow_every_read(guard):
    settings = generate_settings().build(guard=guard)
    allow, ask, deny = (settings["permissions"][k] for k in ("allow", "ask", "deny"))
    assert set(ask) == {PREFIX + name for name in server.WRITE_TOOLS}
    assert not set(ask) & set(allow)
    for name in server.WRITE_TOOLS:
        assert PREFIX + name not in allow
    for name in server.READ_ONLY_TOOLS:
        assert PREFIX + name in allow
    mcp_allowed = {entry for entry in allow if entry.startswith("mcp__")}
    assert mcp_allowed == {PREFIX + name for name in server.READ_ONLY_TOOLS}
    assert not any("*" in entry for entry in mcp_allowed)  # no wildcard that could cover a write tool
    assert {"Bash(sf:*)", "Bash(sfdx:*)", "Bash(npx sf:*)"} <= set(deny)
    assert settings["enabledMcpjsonServers"] == ["sf-investigator"]
    assert not any(entry.startswith("Bash") and ("sf " in entry or "sf:" in entry) for entry in allow)


def test_guarded_settings_protect_the_safety_files_and_install_the_hook():
    module = generate_settings()
    guarded, bare = module.build(guard=True), module.build(guard=False)
    deny = set(guarded["permissions"]["deny"])
    for path in ("/sfti.config.json", "/.claude/settings.json", "/.claude/hooks/**", "/.mcp.json"):
        assert f"Edit({path})" in deny and f"Write({path})" in deny
    matchers = {entry["matcher"] for entry in guarded["hooks"]["PreToolUse"]}
    assert matchers == {"Bash", "Edit|Write|MultiEdit|NotebookEdit"}
    commands = {h["command"] for entry in guarded["hooks"]["PreToolUse"] for h in entry["hooks"]}
    assert commands == {module.HOOK_COMMAND}
    assert module.HOOK_COMMAND.startswith('[ -f "$CLAUDE_PROJECT_DIR/.claude/hooks/guard.py" ] || exit 0;')
    assert "hooks" not in bare
    assert guarded["permissions"]["allow"] == bare["permissions"]["allow"]
    assert guarded["permissions"]["ask"] == bare["permissions"]["ask"]


def test_check_logic(tmp_path, monkeypatch, capsys):
    module = generate_settings()
    target = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr(module, "TARGET", target)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    assert module.main(["--check"]) == 1  # missing file
    assert module.main([]) == 0
    assert json.loads(target.read_text()) == module.build(guard=True)
    assert module.main(["--check"]) == 0
    assert module.main(["--check", "--no-guard"]) == 1  # the file has the guard, the request does not
    assert module.main(["--no-guard"]) == 0
    assert "hooks" not in json.loads(target.read_text())
    assert module.main(["--check", "--no-guard"]) == 0 and module.main(["--check"]) == 1
    target.write_text(target.read_text().replace(PREFIX + "deploy_component", PREFIX + "deploy_component_x"))
    assert module.main(["--check", "--no-guard"]) == 1
    assert "out of date" in capsys.readouterr().out


def test_committed_settings_never_allow_a_write_tool_without_asking():
    """Holds for the file in the repository whether or not it has been regenerated."""
    committed = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    allow, ask, deny = (committed["permissions"][k] for k in ("allow", "ask", "deny"))
    for name in server.WRITE_TOOLS:
        assert PREFIX + name in ask and PREFIX + name not in allow
    known = {PREFIX + name for name in server.READ_ONLY_TOOLS}
    assert {entry for entry in allow if entry.startswith("mcp__")} <= known  # nothing unknown, no wildcard
    assert {"Bash(sf:*)", "Bash(sfdx:*)"} <= set(deny)


def test_committed_settings_match_the_server():
    """The settings in the repository are the generator's output for the server's current tools.

    If this fails, the file is out of date: run ``python scripts/generate_settings.py`` (it also turns
    on the guard hook and the edit protection) and commit the result. See docs/DECISIONS.md, decision 19.
    """
    committed = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    module = generate_settings()
    if committed in (module.build(guard=True), module.build(guard=False), module.build(guard=True, protect_code=True)):
        return
    # Before decision 103 the generator also allowed pytest and the simulator without asking. Only the owner
    # regenerates the file; until then, say so instead of failing every run.
    removed = ["Bash(python -m pytest:*)", "Bash(python3 -m pytest:*)", "Bash(python -m simulator:*)",
               "Bash(python3 -m simulator:*)"]
    for guard in (True, False):
        earlier = module.build(guard=guard)
        earlier["permissions"]["allow"] = earlier["permissions"]["allow"] + removed
        if committed == earlier:
            pytest.xfail("Waiting for the owner: run python scripts/generate_settings.py and commit "
                         ".claude/settings.json (decision 103: pytest and the simulator now ask first).")
    pytest.fail("Run: python scripts/generate_settings.py")


def test_protect_code_denies_edits_to_the_code_that_enforces_the_rules():
    module = generate_settings()
    protected = set(module.build(guard=True, protect_code=True)["permissions"]["deny"])
    for path in ("/core/**", "/mcp_server/**", "/webapp/**", "/scripts/**", "/CLAUDE.md", "/.claude/skills/**"):
        assert f"Edit({path})" in protected and f"Write({path})" in protected
    assert not set(module.DENY_TOOL_CODE) & set(module.build(guard=True)["permissions"]["deny"])
    assert module.build(guard=False, protect_code=True) == module.build(guard=False)  # needs the guard
    allow = module.build(guard=True)["permissions"]["allow"]
    assert not any("pytest" in entry or "simulator" in entry for entry in allow)  # they ask first now


def test_mcp_json_registers_the_server_without_secrets():
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    entry = config["mcpServers"]["sf-investigator"]
    assert entry["args"] == ["-m", "mcp_server"]
    assert "token" not in json.dumps(config).lower() and "password" not in json.dumps(config).lower()


def test_documented_tools_exist():
    """Every tool named in CLAUDE.md's tables is a real tool, and every tool is documented."""
    import re
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    tables = text[text.index("## Tools"):text.index("## Habits")]
    documented = set(re.findall(r"`([a-z_]+)`", tables)) - {"include", "ticket_key", "save_as", "change_id",
                                                             "error", "ok", "sf", "sfdx"}
    documented = {name for name in documented if not name.endswith("_blocked") and not name.endswith("_disabled")}
    assert documented <= set(server.TOOLS), documented - set(server.TOOLS)
    assert set(server.TOOLS) <= documented, set(server.TOOLS) - documented


# --- over stdio -------------------------------------------------------------------------


def test_tool_list_over_stdio_and_calls_end_to_end(sim, project):
    tools, results = over_stdio(sim, project, [
        ("get_config", {}),
        ("org_info", {"org": "prod"}),
        ("soql_query", {"org": "prod", "query": "SELECT Id, Priority, SuppliedEmail, CreatedBy.Name FROM Case",
                        "ticket_key": "T-1", "save_as": "cases", "limit": 4}),
        ("soql_query", {"org": "prod", "query": "DELETE FROM Case"}),
        ("org_info", {"org": "nobody"}),
        ("deploy_component", {"org": "prod", "ticket_key": "T-1", "components": ["ApexClass:CaseTriggerHandler"],
                              "reason": "must be refused"}),
        ("set_trace_flag", {"org": "prod", "user": "005Ws000001IntgIAB"}),
        ("run_apex_tests", {"org": "prod", "class_names": ["CaseEscalationServiceTest"]}),
        ("update_records", {"org": "sandbox", "ticket_key": "T-1", "object_name": "Case",
                            "updates": [{"id": "500Ws00000C0003IAA", "fields": {"Priority": "High"}}],
                            "reason": "advise mode refuses this"}),
        ("audit_log", {}),
    ])
    names = {tool.name for tool in tools}
    assert names == set(server.TOOLS)
    by_name = {tool.name: tool for tool in tools}

    def schema(tool):  # mcp 2.x: input_schema; mcp 1.x: inputSchema
        return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")

    assert schema(by_name["soql_query"])["required"] == ["org", "query"]
    assert set(schema(by_name["deploy_component"])["properties"]) == {
        "org", "ticket_key", "components", "reason", "test_level", "tests", "validate_only"}
    assert all(tool.description for tool in tools)

    config, org, query, bad_query, unknown, deploy, trace, tests, update, audit = results
    assert config["ok"] and config["mode"] == "advise"
    assert org["ok"] and org["classification"] == "production" and org["writes_allowed"] is False
    assert query["ok"] and query["returned"] == 4 and query["saved_to"].endswith("-cases.json")
    assert all(r["SuppliedEmail"] in (None, "[REDACTED]") for r in query["records"])
    assert all(r["CreatedBy"]["Name"] == "[REDACTED]" for r in query["records"])
    assert (project.ticket("T-1") / query["saved_to"]).is_file()
    assert bad_query == {"ok": False, "error": bad_query["error"]} and bad_query["error"]["code"] == "invalid_query"
    assert unknown["ok"] is False and unknown["error"]["code"] == "sf_cli_error" and "hint" in unknown["error"]
    assert deploy["error"]["code"] == "production_write_blocked"
    assert trace["error"]["code"] == "production_write_blocked"
    assert tests["error"]["code"] == "production_test_run_blocked"
    assert update["error"]["code"] == "write_mode_disabled"
    assert [e["outcome"] for e in audit["entries"]] == ["blocked"] * 4
    assert SIMULATED_TOKEN not in json.dumps(results)


def test_wrapper_never_raises_and_reports_bad_arguments(sim, project, monkeypatch):
    monkeypatch.setenv("SFTI_SF_EXECUTABLE", str(sim.launcher))
    monkeypatch.setenv("SFTI_PROJECT_ROOT", str(project.root))
    monkeypatch.setattr(server, "_investigator", None)
    out = json.loads(server.TOOLS["read_file"](ticket_key="../x", path="a"))
    assert out["ok"] is False and out["error"]["code"] == "invalid_input"
    monkeypatch.setattr(server, "investigator", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = json.loads(server.TOOLS["get_config"]())
    assert out == {"ok": False, "error": {"type": "RuntimeError", "code": "internal", "message": "boom"}}


# --- scripts/call_tool.py ---------------------------------------------------------------


def call_tool(sim, project, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "call_tool.py"), *args], capture_output=True,
                          text=True, env=server_env(sim, project), timeout=120)


def test_call_tool_runs_read_only_tools(sim, project):
    listing = call_tool(sim, project, "--list")
    assert listing.returncode == 0 and "org_info" in listing.stdout
    result = call_tool(sim, project, "soql_query", "org=prod", "query=SELECT Id FROM Case", "limit=2")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["returned"] == 2
    extra = call_tool(sim, project, "describe_object", "org=prod", "object_name=Case", "--json",
                      '{"include": ["automation"]}')
    assert json.loads(extra.stdout)["automation"]["triggers"][0]["Name"] == "CaseTrigger"
    failed = call_tool(sim, project, "org_info", "org=nobody")
    assert failed.returncode == 1 and json.loads(failed.stdout)["ok"] is False
    assert call_tool(sim, project, "no_such_tool").returncode == 2
    assert call_tool(sim, project, "org_info", "bogus").returncode == 2
    assert call_tool(sim, project, "org_info", "nope=1").returncode == 2


@pytest.mark.parametrize("name", sorted(EXPECTED_WRITE_TOOLS))
def test_call_tool_refuses_every_write_tool(sim, project, name):
    before = sim.fingerprint("sandbox")
    result = call_tool(sim, project, name, "org=sandbox", "ticket_key=T-1", "user=005Ws000001IntgIAB")
    assert result.returncode == 3 and "write tool" in result.stderr and result.stdout == ""
    assert sim.fingerprint("sandbox") == before
    assert not (project.root / "audit").exists()  # refused before anything was even constructed
