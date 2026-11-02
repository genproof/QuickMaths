"""runtime/tools/bank.py: licensed sources at exact commits, refusals, check and remove (local git repos)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bank", ROOT / "runtime/tools/bank.py")
bank = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bank)
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main"]
APACHE = "                                 Apache License\n                           Version 2.0, January 2004\n"
MIT = "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\n"


def source(path: Path, license_text: str | None) -> str:
    path.mkdir(parents=True)
    subprocess.run([*GIT, "init", "-q", str(path)], check=True)
    (path / "Foo.lean").write_text("theorem foo : True := trivial\n")
    if license_text is not None:
        (path / "LICENSE").write_text(license_text)
    subprocess.run([*GIT, "-C", str(path), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(path), "commit", "-q", "-m", "c"], check=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_add_list_check_remove(tmp_path, capsys):
    rev = source(tmp_path / "up/geo", APACHE)
    wb = tmp_path / "wb"
    assert bank.main(["--workbench", str(wb), "add", "geo", (tmp_path / "up/geo").as_uri(), rev, "--note", "DG"]) == 0
    record = json.loads((wb / "bank/SOURCES.json").read_text())["sources"]
    assert [(s["name"], s["license"], s["lean_files"], s["note"]) for s in record] == [("geo", "Apache-2.0", 1, "DG")]
    assert (wb / "bank/geo/Foo.lean").is_file() and not (wb / "bank/geo/.git/hooks").exists()
    assert bank.main(["--workbench", str(wb), "check"]) == 0
    (wb / "bank/geo/Foo.lean").write_text("changed\n")
    assert bank.main(["--workbench", str(wb), "check"]) == 1
    assert bank.main(["--workbench", str(wb), "remove", "geo"]) == 0
    assert not (wb / "bank/geo").exists() and json.loads((wb / "bank/SOURCES.json").read_text())["sources"] == []
    assert "geo" in capsys.readouterr().out


@pytest.mark.parametrize("license_text, flag, message", [
    (None, None, "no LICENSE or COPYING file"),
    (None, "MIT", "no LICENSE or COPYING file"),
    ("All rights reserved.\n", None, "not recognised"),
    ("GNU GENERAL PUBLIC LICENSE\n", "GPL-3.0", "the source policy allows"),
    (MIT, "Apache-2.0", "is MIT, not Apache-2.0"),
])
def test_refuses_sources_outside_the_licence_policy(tmp_path, license_text, flag, message, capsys):
    rev = source(tmp_path / "up/x", license_text)
    args = ["--workbench", str(tmp_path / "wb"), "add", "x", (tmp_path / "up/x").as_uri(), rev]
    assert bank.main(args + (["--license", flag] if flag else [])) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "wb/bank/x").exists() and not list((tmp_path / "wb/bank").glob(".x.partial"))


def test_mit_detected_and_explicit_licence_for_an_unrecognised_file(tmp_path):
    wb = tmp_path / "wb"
    rev = source(tmp_path / "up/m", MIT)
    assert bank.Bank(wb).add("m", (tmp_path / "up/m").as_uri(), rev, None, "")["license"] == "MIT"
    rev = source(tmp_path / "up/n", "Licensed under the MIT terms, see the header of each file.\n")
    entry = bank.Bank(wb).add("n", (tmp_path / "up/n").as_uri(), rev, "MIT", "")
    assert entry["license"] == "MIT" and entry["license_detected"] is False


def test_bad_names_commits_and_duplicates(tmp_path):
    rev = source(tmp_path / "up/g", APACHE)
    b = bank.Bank(tmp_path / "wb")
    with pytest.raises(bank.Refused, match="source name"):
        b.add("../x", "u", rev, None, "")
    with pytest.raises(bank.Refused, match="reserved"):
        b.add("SOURCES.json", "u", rev, None, "")
    with pytest.raises(bank.Refused, match="40-hex"):
        b.add("g", "u", "main", None, "")
    b.add("g", (tmp_path / "up/g").as_uri(), rev, None, "")
    with pytest.raises(bank.Refused, match="already in the bank"):
        b.add("g", (tmp_path / "up/g").as_uri(), rev, None, "")
