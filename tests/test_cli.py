import pytest

from harness import __version__
from harness.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_doctor_reports_without_raising(tmp_path, capsys):
    code = main(["doctor", "--workbench", str(tmp_path / "missing")])
    out = capsys.readouterr().out
    assert code in (0, 1) and "toolchain" in out and "run `leh workbench`" in out


def test_passthrough_runs_the_component(capsys):
    assert main(["scrub", "--help"]) == 0
    assert main(["bank", "--help"]) == 0


def test_controls_runs_each_case(monkeypatch, tmp_path):
    import harness.cli as cli

    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, env=None: calls.append(cmd) or Done())
    assert main(["controls", "positive", "sorry", "--workbench", str(tmp_path), "--runs", str(tmp_path)]) == 0
    assert [c[3] for c in calls] == ["positive", "sorry"] and calls[0][1:3] == ["-m", "leaneval.controls"]


def test_evolve_extract_quotes_remote_arguments(monkeypatch):
    import harness.cli as cli

    seen = {}

    class Done:
        returncode = 0

    def fake_run(cmd, stdin=None):
        seen["cmd"] = cmd
        return Done()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setenv("LEH_SSH_KNOWN_HOSTS", "/k/known")
    assert main(["evolve", "extract", "host-1", "--own", "dir with space"]) == 0
    assert seen["cmd"][-2:] == ["host-1", "python3 -I - --own 'dir with space'"]
    assert "BatchMode=yes" in seen["cmd"] and "UserKnownHostsFile=/k/known" in seen["cmd"]
    assert main(["evolve", "extract", "--local", "--own", "x"]) == 0
    assert seen["cmd"][1:2] == ["-I"] and seen["cmd"][-2:] == ["--own", "x"]
    assert main(["evolve"]) == 2
