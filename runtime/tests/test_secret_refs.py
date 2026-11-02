"""Secret references: file:/env: resolution, privacy check, account labels, no secret in errors."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from leaneval import secret_refs  # noqa: E402
from leaneval.runtime import RuntimeBlocked  # noqa: E402

TOKEN = "sk-ant-oat01-" + "C" * 95


class SecretRefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.file = self.home / ".config/leh/accounts/account-a.token"
        self.file.parent.mkdir(parents=True)
        self.file.write_text(TOKEN + "\n")
        self.file.chmod(0o600)

    def test_file_reference_expands_home_and_requires_mode_600(self):
        ref = "file:~/.config/leh/accounts/account-a.token"
        self.assertEqual(secret_refs.resolve(ref, self.home), TOKEN)
        self.assertEqual(secret_refs.path_of(ref, self.home), self.file)
        self.file.chmod(0o640)
        with self.assertRaisesRegex(secret_refs.SecretError, "mode 600"):
            secret_refs.resolve(ref, self.home)

    def test_bare_absolute_path_is_a_file_reference(self):
        self.assertEqual(secret_refs.resolve(str(self.file)), TOKEN)

    def test_env_reference(self):
        with mock.patch.dict(os.environ, {"LEH_TEST_TOKEN": TOKEN}):
            self.assertEqual(secret_refs.resolve("env:LEH_TEST_TOKEN"), TOKEN)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(secret_refs.SecretError) as caught:
                secret_refs.resolve("env:LEH_TEST_TOKEN")
            self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIsNone(secret_refs.path_of("env:LEH_TEST_TOKEN"))

    def test_labels(self):
        self.assertEqual(secret_refs.label("file:~/.config/leh/accounts/account-a.token", self.home), "account-a")
        self.assertEqual(secret_refs.label("file:/x/account-a.token-2"), "account-a.token-2")
        self.assertEqual(secret_refs.label("env:LEH_TOKEN_B"), "LEH_TOKEN_B")

    def test_malformed_references(self):
        for ref in ("", "env:", "env:1X", "file:relative/path", "relative", "file:/a\0b"):
            with self.assertRaises(secret_refs.SecretError):
                secret_refs.parse(ref, self.home)

    def test_supervisor_reads_an_env_reference(self):
        from leaneval import agent_runner
        env = {"LEANEVAL_CLAUDE_TOKEN_REF": "env:LEH_TEST_TOKEN", "LEH_TEST_TOKEN": TOKEN}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(agent_runner._read_token(), TOKEN)
            self.assertEqual(agent_runner.token_paths(), [])
            self.assertEqual(agent_runner.account_labels(), ["LEH_TEST_TOKEN"])
        with mock.patch.dict(os.environ, {"LEANEVAL_CLAUDE_TOKEN_REF": "env:LEH_TEST_TOKEN", "LEH_TEST_TOKEN": "x"}):
            with self.assertRaisesRegex(RuntimeBlocked, "unexpected format"):
                agent_runner._read_token()


if __name__ == "__main__":
    unittest.main()
