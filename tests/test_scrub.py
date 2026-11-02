"""tools/scrub.py: every rule fires on a planted value and stays quiet on clean text."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scrub", ROOT / "tools" / "scrub.py")
scrub = importlib.util.module_from_spec(spec)
sys.modules["scrub"] = scrub
spec.loader.exec_module(scrub)

# Planted values are assembled at run time so this file never contains them literally.
PLANTED = {
    "anthropic_key": "token = " + "sk-" + "ant-" + "A" * 24,
    "github_token": "gh" + "p_" + "B" * 36,
    "aws_access_key": "AK" + "IA" + "C" * 16,
    "private_key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "url_credentials": "https://user:" + "hunter2" + "@example.org/x",
    "credential_path": "cat ~/.config/leh/" + "oauth-" + "token",
    "host_path": "cd " + "/ho" + "me/alice/project",
    "email": "contact " + "alice" + "@" + "corp.example.io",
    "ipv4": "ssh 10." + "1.2.3",
}


@pytest.mark.parametrize("rule,line", sorted(PLANTED.items()))
def test_rule_fires(rule, line):
    assert rule in {f.rule for f in scrub.scan_text("x.py", line, [])}


@pytest.mark.parametrize("line", [
    "plain text with version 1.2.3 and path src/harness/core.py",
    "Co-Authored-By: Claude <noreply@anthropic.com>",
    "author 1+bot@users.noreply.github.com",
    "listen on 127.0.0.1:8080 or 0.0.0.0",
    "docs example 192.0.2.10",
])
def test_clean_lines(line):
    assert scrub.scan_text("x.md", line, []) == []


def test_allow_marker_skips_line():
    line = PLANTED["anthropic_key"] + "  # " + "scrub: " + "allow"
    assert scrub.scan_text("x.py", line, []) == []


def test_denylist_is_case_insensitive_and_covers_paths():
    assert {f.rule for f in scrub.scan_text("a.md", "runner on HostAlpha", ["hostalpha"])} == {"denylist"}
    assert "denylist_path" in {f.rule for f in scrub.scan_blob("docs/hostalpha.md", b"x", ["hostalpha"])}


def test_file_rules():
    assert {f.rule for f in scrub.scan_blob("data/rounds.jsonl", b"{}", [])} == {"forbidden_file"}
    assert {f.rule for f in scrub.scan_blob("x/participants.json", b"{}", [])} == {"forbidden_file"}
    assert {f.rule for f in scrub.scan_blob("big.txt", b"a" * (scrub.MAX_BYTES + 1), [])} == {"large_file"}
    assert scrub.scan_blob("bin.dat", b"\0\1" + b"sk-ant-" + b"A" * 30, []) == []  # binary: file rules only


def test_findings_never_print_matched_text(capsys, tmp_path):
    secret = "sk-" + "ant-" + "Z" * 30
    target = tmp_path / "leak.txt"
    target.write_text(f"key={secret}\n")
    assert scrub.main([str(target), "--denylist", str(tmp_path / "none")]) == 1
    err = capsys.readouterr().err
    assert "anthropic_key" in err and secret not in err


def test_author_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    for email, ok in (("1+me@users.noreply.github.com", True), ("person" + "@" + "mail.example.io", False)):
        env = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": "x",
               "GIT_COMMITTER_EMAIL": email, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "c"], check=True, env=env)
        assert (scrub.author_findings() == []) is ok


def test_history_finds_a_secret_removed_later_and_a_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "1+me@users.noreply.github.com", "GIT_COMMITTER_NAME": "x",
           "GIT_COMMITTER_EMAIL": "1+me@users.noreply.github.com", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    subprocess.run(["git", "init", "-q"], check=True)
    (tmp_path / "a.txt").write_text("key = " + "sk-" + "ant-" + "Q" * 30 + "\n")
    subprocess.run(["git", "add", "a.txt"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], check=True, env=env)
    (tmp_path / "a.txt").write_text("clean\n")
    subprocess.run(["git", "commit", "-q", "-am", "deploy on hostalpha"], check=True, env=env)
    found = scrub.history_findings(["hostalpha"])
    assert {f.rule for f in found} == {"anthropic_key", "denylist"}
    assert any(f.path.startswith("a.txt@") for f in found) and any("message" in f.path for f in found)
