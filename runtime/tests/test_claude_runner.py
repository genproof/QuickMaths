from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from leaneval import agent_runner
from leaneval.agent_runner import StreamAccount, build_command, _run_locked

def run_job(job, *, resume=False):
    """Exercise provider supervision; broker transport has separate integration tests."""
    return _run_locked(job, resume=resume)
from leaneval.runtime import RunLedger, RuntimeBlocked

FAKE_LANDRUN = r'''#!/usr/bin/env python3
import os, sys
args = [a for a in sys.argv[1:] if a != "--best-effort"]
keys = []
while args and args[0] != "--":
    flag = args.pop(0)
    if flag in ("--rox", "--ro", "--rw", "--rwx", "--connect-tcp", "--bind-tcp"):
        args.pop(0)
    elif flag == "--env":
        keys.append(args.pop(0))
args.pop(0)
env = {k: os.environ[k] for k in keys if k in os.environ}
os.execve(args[0], args, env)
'''

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys, time
argv = sys.argv[1:]
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
session = opt("--session-id") or opt("--resume")
prompt = sys.stdin.read()
assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
cred = json.load(open(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".credentials.json")))
assert cred["claudeAiOauth"]["accessToken"].startswith("sk-ant-oat01-")
assert oct(os.stat(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".credentials.json")).st_mode & 0o777) == "0o600"
requested = opt("--model")
assert (requested, opt("--effort")) in {("claude-opus-5-5", "xhigh"), ("claude-sonnet-5-5", "max")}
assert opt("--permission-mode") == "bypassPermissions"
assert opt("--allowedTools") == "Bash,Read,Edit,Write,Glob,Grep"
assert opt("--permission-prompts") == "none"
state_path = os.environ["LEANEVAL_FAKE_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {"calls": 0, "prompts": []}
state["calls"] += 1
state["prompts"].append(prompt)
state.setdefault("resumes", []).append("--resume" in argv)
state.setdefault("models", []).append(requested)
state.setdefault("tokens", []).append(cred["claudeAiOauth"]["accessToken"][-1])
state.setdefault("compact", []).append(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
json.dump(state, open(state_path, "w"))
scenario = os.environ["LEANEVAL_FAKE_SCENARIO"]
call = state["calls"]
model = "claude-haiku-4-5" if scenario == "wrong_model" else requested
effort = "xhigh" if model == "claude-opus-5-5" else "max"
home = os.environ["CLAUDE_CONFIG_DIR"]
os.makedirs(os.path.join(home, "projects", "-ws"), exist_ok=True)
transcript = open(os.path.join(home, "projects", "-ws", session + ".jsonl"), "a")
def emit(value):
    print(json.dumps(value), flush=True)
def message(index, usage, text="working"):
    mid = "msg_%d_%d" % (call, index)
    entry = {"type": "assistant", "effort": effort, "perTurnEffort": effort,
             "isSidechain": False, "sessionId": session, "message": {"model": model, "id": mid, "usage": usage}}
    transcript.write(json.dumps(entry) + "\n"); transcript.flush()
    for part in ("thinking", "text"):
        emit({"type": "assistant", "message": {"id": mid, "model": model, "usage": usage,
                                               "content": [{"type": part, "text": text}]}})
def result(text, is_error=False, usage=None, code=0):
    emit({"type": "result", "subtype": "success", "is_error": is_error, "result": text,
          "usage": usage or {}, "modelUsage": {}, "total_cost_usd": 0.01,
          "session_id": session})
    sys.exit(code)
emit({"type": "system", "subtype": "init", "model": model, "cwd": os.getcwd(),
      "permissionMode": "bypassPermissions", "claude_code_version": "2.1.289",
      "session_id": session, "tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
      "apiKeySource": "none"})
emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
      "resetsAt": int(time.time()) + 3600, "unifiedWindows": {"five_hour": {
      "utilization": 0.1, "resetsAt": int(time.time()) + 3600}}}})
u = {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 20,
     "output_tokens": 3}
if scenario in ("ready", "wrong_model"):
    message(1, u)
    if scenario == "wrong_model":
        time.sleep(5)
    result("All holes proved. READY_FOR_VERIFICATION", usage=u)
if scenario == "nudge":
    message(1, u)
    result("done for now" if call == 1 else "READY_FOR_VERIFICATION", usage=u)
if scenario == "rate_limit_both":
    if call in (1, 2):
        message(1, u)
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
              "resetsAt": int(time.time()) + 1, "rateLimitType": "five_hour"}})
        result("You've hit your limit", is_error=True, usage=u, code=1)
    message(1, u)
    result("READY_FOR_VERIFICATION", usage=u)
if scenario == "rate_limit":
    if call == 1:
        message(1, u)
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
              "resetsAt": int(time.time()) + 1, "rateLimitType": "five_hour"}})
        result("Claude usage limit reached. Your limit will reset at 11:40.", is_error=True,
               usage=u, code=1)
    message(1, u)
    result("READY_FOR_VERIFICATION", usage=u)
if scenario == "reserve":
    if call == 1:
        message(1, u)
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning",
              "resetsAt": int(time.time()) + 2, "rateLimitType": "five_hour",
              "unifiedWindows": {"five_hour": {"utilization": 0.93, "resetsAt": time.time() + 1.5},
                                 "seven_day": {"utilization": 0.5, "resetsAt": time.time() + 9e4}}}})
        while True:
            message(2, u)
            time.sleep(0.2)
    message(1, u)
    result("READY_FOR_VERIFICATION", usage=u)
if scenario == "rate_limit_two":
    if call in (1, 2):
        message(1, u)
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
              "resetsAt": int(time.time()) + 1, "rateLimitType": "five_hour"}})
        result("You've hit your limit", is_error=True, usage=u, code=1)
    message(1, u)
    result("READY_FOR_VERIFICATION", usage=u)
if scenario == "auth":
    result("Invalid API key · Please run /login", is_error=True, code=1)
if scenario == "compaction":
    message(1, u)
    bigger = dict(u, input_tokens=1010)
    result("READY_FOR_VERIFICATION", usage=bigger)
if scenario == "credentials":
    mid = "msg_%d_1" % call
    emit({"type": "assistant", "message": {"id": mid, "model": model, "usage": u, "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat $CLAUDE_CONFIG_DIR/.credentials.json"}}]}})
    while True:
        message(2, u)
        time.sleep(0.2)
if scenario in ("tokens", "wall"):
    index = 0
    while True:
        index += 1
        message(index, u if scenario == "tokens" else {"input_tokens": 1, "output_tokens": 0})
        time.sleep(0.05 if scenario == "tokens" else 0.5)
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # Provider supervision tests isolate environment preflight, covered by its own suite.
    monkeypatch.setattr(agent_runner.research, "prepare", lambda *args: "")
    home = tmp_path / "home"
    (home / ".config/leh/accounts").mkdir(parents=True)
    token = home / ".config/leh/accounts/account-a.token"
    token.write_text("sk-ant-oat01-" + "A" * 95)
    token.chmod(0o600)
    tools = tmp_path / "tools"
    tools.mkdir()
    landrun = tools / "landrun"
    landrun.write_text(FAKE_LANDRUN.replace("/usr/bin/env python3", sys.executable))
    landrun.chmod(0o755)
    claude = tools / "claude"
    claude.write_text(FAKE_CLAUDE.replace("/usr/bin/env python3", sys.executable))
    claude.chmod(0o755)
    monkeypatch.setenv("LEANEVAL_QUOTA_STATE", str(tmp_path / "quota-state"))
    monkeypatch.setenv("LEANEVAL_CLAUDE_STATE", str(tmp_path / "claude-state"))
    monkeypatch.setenv("LEANEVAL_HOME", str(home))
    monkeypatch.setenv("LEANEVAL_LANDRUN", str(landrun))
    monkeypatch.setenv("LEANEVAL_CLAUDE_BIN", str(claude))
    monkeypatch.setenv("LEANEVAL_CLAUDE_SHA256", hashlib.sha256(claude.read_bytes()).hexdigest())
    monkeypatch.setenv("LEANEVAL_CLAUDE_TOKEN_FILE", str(token))
    monkeypatch.setenv("LEANEVAL_FAKE_STATE", str(tmp_path / "fake-state.json"))
    monkeypatch.setattr(agent_runner, "RESET_MARGIN", 0.5)
    monkeypatch.setattr(agent_runner, "TRANSIENT_BACKOFF", (0.1, 0.1, 0.1))
    return tmp_path


def make_job(root: Path, *, wall=30, tokens=10_000, assignment=None) -> Path:
    job = root / "job"
    (job / "workspace").mkdir(parents=True)
    (job / "workspace/Submission.lean").write_text("theorem fixture : True := by trivial\n")
    (job / "prompt.txt").write_text("prove locally")
    config = {"run_id": "r1", "wall_seconds": wall, "token_limit": tokens}
    if assignment is not None:
        config["campaign_assignment"] = assignment
    (job / "config.json").write_text(json.dumps(config))
    return job


def scenario(monkeypatch, name):
    monkeypatch.setenv("LEANEVAL_FAKE_SCENARIO", name)


def fake_state(root: Path) -> dict:
    return json.loads((root / "fake-state.json").read_text())


def test_command_is_sandboxed_pinned_and_never_exposes_token(env, tmp_path):
    job = make_job(env)
    command = build_command(job, "0" * 8 + "-0000-0000-0000-" + "0" * 12, resume=False,
                            agent=Path(agent_runner.__file__).resolve().parents[1],
                            config_dir=job / "claude-home",
                            env_keys=["CLAUDE_CODE_OAUTH_TOKEN", "PATH"])
    joined = " ".join(command)
    assert "sk-ant" not in joined
    assert command[command.index("--") + 1].endswith("claude")
    assert command[command.index("--model") + 1] == "claude-opus-5-5"
    assert command[command.index("--effort") + 1] == "xhigh"
    assert command[command.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
    assert "--strict-mcp-config" in command and "--session-id" in command
    rwx = [command[i + 1] for i, v in enumerate(command) if v == "--rwx"]
    rw = [command[i + 1] for i, v in enumerate(command) if v == "--rw"]
    assert str(job / "workspace") in rwx
    assert str(job / "claude-home") not in rw  # created later by the supervisor
    assert all(not p.startswith(str(env / "home" / ".config")) for p in rwx + rw)


def test_ready_turn_is_verifier_ready_with_native_evidence(env, monkeypatch):
    scenario(monkeypatch, "ready")
    job = make_job(env)
    result = run_job(job)
    assert result["status"] == "awaiting_verifier"
    assert result["observed_tokens"] == 38  # duplicated stream events are counted once
    assert result["usage"]["input_tokens"] == 35 and result["usage"]["cached_input_tokens"] == 20
    assert result["effective_config"]["model"] == "claude-opus-5-5"
    assert result["effective_config"]["reasoning_effort"] == "xhigh"
    assert result["configuration_confirmed"] and result["ready_marker"]
    assert result["usd"] is None and result["verified"] is False
    checkpoint = json.loads((job / "checkpoint.json").read_text())
    assert checkpoint["safe_resume"] and checkpoint["thread_id"] == result["thread_id"]
    assert not (Path(result["cli_state_directory"]) / "claude-home/.credentials.json").exists()
    assert not str(result["cli_state_directory"]).startswith(str(job))
    assert "sk-ant" not in (job / "events.jsonl").read_text()
    with pytest.raises(RuntimeBlocked, match="explicit"):
        run_job(job)


def test_early_turn_end_is_nudged_in_same_session(env, monkeypatch):
    scenario(monkeypatch, "nudge")
    job = make_job(env)
    result = run_job(job)
    state = fake_state(env)
    assert state["calls"] == 2 and state["resumes"] == [False, True]
    assert "Controller note" in state["prompts"][1]
    assert result["nudges"] == 1 and result["status"] == "awaiting_verifier"
    assert result["observed_tokens"] == 76


def test_usage_limit_on_the_only_account_waits_for_opus(env, monkeypatch):
    monkeypatch.setattr(agent_runner, "MIN_LIMIT_PAUSE", 0.5)
    scenario(monkeypatch, "rate_limit")
    job = make_job(env)
    result = run_job(job)
    state = fake_state(env)
    assert state["calls"] == 2 and state["resumes"] == [False, True]
    assert state["models"] == ["claude-opus-5-5", "claude-opus-5-5"]
    assert "usage-limit pause" in state["prompts"][1]
    assert result["status"] == "awaiting_verifier"
    assert [p["reason"] for p in result["pauses"]] == ["usage_limit_pause"]
    assert result["invocations"][0]["outcome"] == "usage_limit"
    assert "switch_to" not in result["invocations"][0]
    assert result["effective_config"]["fallback_used"] is False
    assert result["configuration_confirmed"] is True
    assert result["observed_tokens"] == 76


def test_repeated_limit_keeps_waiting_for_opus(env, monkeypatch):
    monkeypatch.setattr(agent_runner, "MIN_LIMIT_PAUSE", 0.5)
    scenario(monkeypatch, "rate_limit_both")
    job = make_job(env)
    result = run_job(job)
    state = fake_state(env)
    assert state["models"] == ["claude-opus-5-5"] * 3
    assert [p["reason"] for p in result["pauses"]] == ["usage_limit_pause"] * 2
    assert result["status"] == "awaiting_verifier"
    assert result["elapsed_seconds"] < result["real_elapsed_seconds"]


def reserve_switch(value):
    path = agent_runner.state_root() / agent_runner.RESERVE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"utilization": value}))


def test_reserve_threshold_leaves_window_to_coordinator_then_resumes(env, monkeypatch):
    monkeypatch.setattr(agent_runner, "MIN_LIMIT_PAUSE", 0.5)
    scenario(monkeypatch, "reserve")
    reserve_switch(0.9)
    job = make_job(env)
    result = run_job(job)
    state = fake_state(env)
    assert state["calls"] == 2 and state["resumes"] == [False, True]
    assert state["models"] == ["claude-opus-5-5", "claude-opus-5-5"]
    assert "usage-limit pause" in state["prompts"][1]
    assert [p["reason"] for p in result["pauses"]] == ["coordinator_reserve_pause"]
    assert result["invocations"][0]["outcome"] == "coordinator_reserve"
    assert result["status"] == "awaiting_verifier" and not result["usage_invalid"]
    assert result["requested_config"]["coordinator_reserve_switch"] == "coordinator-reserve.json"
    assert result["elapsed_seconds"] < result["real_elapsed_seconds"]


def test_without_reserve_switch_workers_use_the_whole_window(env, monkeypatch):
    scenario(monkeypatch, "reserve")
    result = run_job(make_job(env, wall=3))
    assert fake_state(env)["calls"] == 1
    assert result["pauses"] == [] and result["stop_reason"] == "wall_limit"


def test_shared_reserve_observation_delays_launch(env, monkeypatch):
    monkeypatch.setattr(agent_runner, "MIN_LIMIT_PAUSE", 0.5)
    scenario(monkeypatch, "ready")
    reserve_switch(0.9)
    share = agent_runner.rate_limit_share()
    share.parent.mkdir(parents=True, exist_ok=True)
    share.write_text(json.dumps({"unifiedWindows": {
        "seven_day": {"utilization": 0.95, "resetsAt": time.time() + 1}}}))
    from leaneval import quota
    observed = json.loads(share.read_text())
    wake = observed["unifiedWindows"]["seven_day"]["resetsAt"]
    quota.current().observe(quota.group_for("account-a"), observed, origin="isolated-test", margin=0)
    result = run_job(make_job(env))
    assert fake_state(env)["calls"] == 1
    assert result["pauses"] and result["pauses"][0]["ended_at"] >= wake
    assert result["status"] == "awaiting_verifier"
    assert json.loads(share.read_text())["unifiedWindows"]["five_hour"]["utilization"] == 0.1


def test_reserve_until_takes_latest_future_window_at_threshold():
    info = {"unifiedWindows": {"five_hour": {"utilization": 0.9, "resetsAt": 2000},
                               "seven_day": {"utilization": 0.95, "resetsAt": 9000}}}
    assert agent_runner.reserve_until(info, 1000.0, 0.9) == 9000
    assert agent_runner.reserve_until(info, 9500.0, 0.9) is None
    assert agent_runner.reserve_until(info, 1000.0, None) is None
    low = {"unifiedWindows": {"five_hour": {"utilization": 0.89, "resetsAt": 2000}}}
    assert agent_runner.reserve_until(low, 1000.0, 0.9) is None
    assert agent_runner.reserve_until({"unifiedWindows": {"x": None}}, 1000.0, 0.9) is None


def second_account(env):
    token = env / "home/.config/leh/accounts/account-a.token-2"
    token.write_text("sk-ant-oat01-" + "B" * 95 + "\n")
    token.chmod(0o600)


def test_usage_limit_moves_to_next_account_on_opus(env, monkeypatch):
    scenario(monkeypatch, "rate_limit")
    second_account(env)
    result = run_job(make_job(env))
    state = fake_state(env)
    assert state["models"] == ["claude-opus-5-5", "claude-opus-5-5"]
    assert state["tokens"] == ["A", "B"] and state["resumes"] == [False, True]
    assert result["invocations"][0]["switch_account_to"] == "account-a.token-2"
    assert result["invocations"][1]["account"] == "account-a.token-2"
    assert result["pauses"] == [] and result["status"] == "awaiting_verifier"
    assert result["requested_config"]["subscription_accounts"] == [
        "account-a", "account-a.token-2"]
    shared = json.loads(agent_runner.account_limits_path().read_text())
    assert "account-a|claude-opus-5-5" in shared


def test_every_account_refused_waits_for_opus(env, monkeypatch):
    monkeypatch.setattr(agent_runner, "MIN_LIMIT_PAUSE", 0.5)
    scenario(monkeypatch, "rate_limit_two")
    second_account(env)
    result = run_job(make_job(env))
    state = fake_state(env)
    assert state["models"] == ["claude-opus-5-5"] * 3
    assert state["tokens"] == ["A", "B", "A"]
    assert [p["reason"] for p in result["pauses"]] == ["usage_limit_pause"]
    assert result["status"] == "awaiting_verifier"


def test_previous_round_notes_are_carried(env, monkeypatch):
    scenario(monkeypatch, "ready")
    parent = env / "parent"
    (parent / "workspace").mkdir(parents=True)
    (parent / "workspace/NOTES.md").write_text("## HANDOFF\nnext: prove X\n")
    job = make_job(env)
    config = json.loads((job / "config.json").read_text())
    config["continuation_provenance"] = {"parent_job": str(parent)}
    (job / "config.json").write_text(json.dumps(config))
    result = run_job(job)
    assert result["status"] == "awaiting_verifier"
    assert (job / "workspace/NOTES.prev.md").read_text() == "## HANDOFF\nnext: prove X\n"
    assert "NOTES.prev.md" in fake_state(env)["prompts"][0]


def test_fresh_job_gets_no_previous_notes(env, monkeypatch):
    scenario(monkeypatch, "ready")
    job = make_job(env)
    run_job(job)
    assert not (job / "workspace/NOTES.prev.md").exists()
    assert "NOTES.prev.md" not in fake_state(env)["prompts"][0]


def test_host_block_skips_an_exhausted_account(env, monkeypatch):
    scenario(monkeypatch, "ready")
    second_account(env)
    path = agent_runner.account_limits_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"account-a|claude-opus-5-5": time.time() + 100}))
    result = run_job(make_job(env))
    state = fake_state(env)
    assert state["tokens"] == ["B"] and state["models"] == ["claude-opus-5-5"]
    assert result["status"] == "awaiting_verifier"


def test_assignment_sets_compaction_window_and_account_order(env, monkeypatch):
    scenario(monkeypatch, "ready")
    second_account(env)
    job = make_job(env, assignment={"auto_compact_window": 200000,
                                    "account_order": ["account-a.token-2"]})
    result = run_job(job)
    state = fake_state(env)
    assert state["compact"] == ["200000"] and state["tokens"] == ["B"]
    assert result["requested_config"]["auto_compact_window"] == 200000
    assert result["requested_config"]["account_order"][0] == "account-a.token-2"


def test_no_compaction_window_by_default(env, monkeypatch):
    scenario(monkeypatch, "ready")
    run_job(make_job(env, assignment={"auto_compact_window": 10}))
    assert fake_state(env)["compact"] == [None]


def test_token_threshold_stops_and_counts_cache_reads(env, monkeypatch):
    scenario(monkeypatch, "tokens")
    job = make_job(env, tokens=400)
    result = run_job(job)
    assert result["stop_reason"] == "token_limit"
    assert result["status"] == "interrupted_unknown_usage"
    assert result["observed_tokens"] >= 400
    final = [e["data"] for e in RunLedger(job / "ledger.sqlite3").events()
             if e["kind"] == "result"][-1]["input"]
    assert final["ambiguous"] and final["result"]["reason"] == "token_limit"
    assert type(final["result"]["exit_code"]) is int


def test_wall_threshold_stops(env, monkeypatch):
    scenario(monkeypatch, "wall")
    job = make_job(env, wall=3)
    result = run_job(job)
    assert result["stop_reason"] == "wall_limit"
    assert 3 <= result["elapsed_seconds"] < 25


def test_wrong_served_model_is_stopped_as_mismatch(env, monkeypatch):
    scenario(monkeypatch, "wrong_model")
    job = make_job(env)
    result = run_job(job)
    assert result["stop_reason"] == "configuration_mismatch"
    assert result["status"] != "awaiting_verifier"


def test_authentication_failure_is_not_retried(env, monkeypatch):
    scenario(monkeypatch, "auth")
    job = make_job(env)
    result = run_job(job)
    assert fake_state(env)["calls"] == 1
    assert result["stop_reason"] == "authentication_failure"


def test_unstreamed_result_usage_is_charged(env, monkeypatch):
    scenario(monkeypatch, "compaction")
    job = make_job(env)
    result = run_job(job)
    assert result["observed_tokens"] == 1038


def test_token_file_must_be_private(env, monkeypatch):
    scenario(monkeypatch, "ready")
    (env / "home/.config/leh/accounts/account-a.token").chmod(0o644)
    with pytest.raises(RuntimeBlocked, match="mode 600"):
        run_job(make_job(env))


def test_pinned_binary_checksum_is_enforced(env, monkeypatch):
    scenario(monkeypatch, "ready")
    monkeypatch.setenv("LEANEVAL_CLAUDE_SHA256", "0" * 64)
    with pytest.raises(RuntimeBlocked, match="checksum"):
        run_job(make_job(env))


def test_stream_account_takes_message_maxima_and_result_floor():
    account = StreamAccount()
    account.begin_invocation()
    account.message("m1", {"input_tokens": 1, "output_tokens": 2})
    account.message("m1", {"input_tokens": 1, "output_tokens": 7})
    assert account.tokens == 8
    account.result({"input_tokens": 1, "output_tokens": 7, "cache_read_input_tokens": 10}, {})
    account.close_invocation(has_result=True)
    assert account.tokens == 18
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") is None


def test_reaching_for_the_credentials_ends_the_round(env, monkeypatch):
    scenario(monkeypatch, "credentials")
    job = make_job(env)
    result = run_job(job)
    assert result["stop_reason"] == "credential_access"
    assert result["security_event"]["tool"] == "Bash" and result["security_event"]["kind"] == "credential_access"
    assert json.loads((job / "SECURITY-EVENT.json").read_text())["tool"] == "Bash"
    assert not (Path(result["cli_state_directory"]) / "claude-home/.credentials.json").exists()
    assert fake_state(env)["calls"] == 1  # never resumed or nudged


@pytest.mark.parametrize("block, hit", [
    ({"type": "tool_use", "name": "Bash", "input": {"command": "cat ~/.claude/.credentials.json"}}, True),
    ({"type": "tool_use", "name": "Bash", "input": {"command": "tr '\\0' '\\n' < /proc/$PPID/environ"}}, True),
    ({"type": "tool_use", "name": "Bash", "input": {"command": "printenv LEAN_PATH"}}, False),
    ({"type": "tool_use", "name": "Bash", "input": {"command": "ls /x/state/claude-home/xdg/cache/mathlib"}}, False),
    ({"type": "tool_use", "name": "Bash", "input": {"command": "cat /x/state/claude-home/.claude.json"}}, True),
    ({"type": "tool_use", "name": "Read", "input": {"file_path": "/x/claude-home/settings.json"}}, True),
    ({"type": "tool_use", "name": "Glob", "input": {"pattern": "**/.credentials*"}}, True),
    ({"type": "tool_use", "name": "Bash", "input": {"command": "./lean build Submission && lake env lean x.lean"}}, False),
    ({"type": "tool_use", "name": "Grep", "input": {"pattern": "credentials", "path": "Submission"}}, False),
    ({"type": "tool_use", "name": "Write", "input": {"file_path": "NOTES.md", "content": "no .credentials here"}}, False),
    ({"type": "text", "text": "cat .credentials.json"}, False),
])
def test_credential_access_patterns(tmp_path, block, hit):
    assert (agent_runner.credential_access(block, Path("/x/state/claude-home")) is not None) is hit
