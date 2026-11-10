"""Tests for scripts/verify_change.sh, run against throwaway git repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_change.sh"
BASH = shutil.which("bash")
GIT = shutil.which("git")
OWNER = ("Owner Name", "1234+owner@users.noreply.github.com")
BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")


@unittest.skipUnless(BASH and GIT, "needs bash and git on PATH")
class VerifyChangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        empty_config = root / "gitconfig"
        empty_config.write_text("", encoding="utf-8")
        # Isolate from the machine's git config (autocrlf, hooks, signing...).
        self.env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(empty_config),
        }
        self.git("init", "-q", "-b", "main")
        self.write("app.py", "print('hello')\n")
        self.base = self.commit("Initial commit")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        proc = subprocess.run(
            [GIT, *args], cwd=self.repo, env={**self.env, **(env or {})},
            capture_output=True, text=True, check=True,
        )
        return proc.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, message: str, author: tuple[str, str] = OWNER,
               committer: tuple[str, str] | None = None) -> str:
        committer = committer or author
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message, env={
            "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1],
        })
        return self.git("rev-parse", "HEAD")

    def verify(self, new: str, base: str | None = None, **env: str) -> subprocess.CompletedProcess[str]:
        full_env = {
            **self.env,
            "EXPECT_NAME": OWNER[0],
            "EXPECT_EMAIL": OWNER[1],
            "MAX_CHANGED_LINES": "50",
            "ALLOW_WORKFLOW_EDITS": "true",
            **env,
        }
        return subprocess.run(
            [BASH, SCRIPT.as_posix(), base or self.base, new],
            cwd=self.repo, env=full_env, capture_output=True, text=True, check=False,
        )

    def assertPasses(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assertFails(self, result: subprocess.CompletedProcess[str], fragment: str) -> None:
        self.assertNotEqual(result.returncode, 0, "expected the gate to reject the change")
        self.assertIn(fragment, result.stdout + result.stderr)

    # -- tests ------------------------------------------------------------

    def test_clean_change_passes_and_reports_size(self) -> None:
        self.write("app.py", "print('hello, world')\n")
        result = self.verify(self.commit("Greet the world"))
        self.assertPasses(result)
        self.assertIn("changed_lines=2", result.stdout)

    def test_bot_author_is_rejected(self) -> None:
        self.write("app.py", "print('bot')\n")
        self.assertFails(self.verify(self.commit("Bot change", author=BOT)), "authored by")

    def test_other_committer_is_rejected(self) -> None:
        self.write("app.py", "print('x')\n")
        self.assertFails(self.verify(self.commit("Change", committer=BOT)), "committed by")

    def test_co_author_trailer_is_rejected(self) -> None:
        self.write("app.py", "print('pair')\n")
        new = self.commit("Pair change\n\nCo-authored-by: Someone <someone@example.com>")
        self.assertFails(self.verify(new), "Co-authored-by")

    def test_closing_keyword_in_message_is_rejected(self) -> None:
        self.write("app.py", "print('fixed')\n")
        self.assertFails(self.verify(self.commit("Fix crash\n\nThis fixes #3.")), "closing keyword")

    def test_defused_closing_keyword_passes(self) -> None:
        self.write("app.py", "print('fixed')\n")
        self.assertPasses(self.verify(self.commit("Fix crash\n\nThis fixes `#3`; see #4.")))

    def test_large_file_is_rejected(self) -> None:
        (self.repo / "blob.bin").write_bytes(b"\0" * 4096)
        self.assertFails(self.verify(self.commit("Add a blob"), MAX_FILE_BYTES="1024"), "larger than 1024 bytes")

    def test_two_commits_are_rejected(self) -> None:
        self.write("a.py", "a = 1\n")
        self.commit("One")
        self.write("b.py", "b = 2\n")
        self.assertFails(self.verify(self.commit("Two")), "exactly one commit directly on top of")

    def test_merge_commit_is_rejected(self) -> None:
        self.git("checkout", "-q", "-b", "side")
        self.write("side.py", "s = 1\n")
        self.commit("Side")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "--no-edit", "side", env={
            "GIT_AUTHOR_NAME": OWNER[0], "GIT_AUTHOR_EMAIL": OWNER[1],
            "GIT_COMMITTER_NAME": OWNER[0], "GIT_COMMITTER_EMAIL": OWNER[1],
        })
        self.assertFails(self.verify(self.git("rev-parse", "HEAD")), "single non-merge commit")

    def test_change_must_sit_on_the_base(self) -> None:
        self.write("a.py", "a = 1\n")
        moved = self.commit("Main moved on")
        self.write("b.py", "b = 2\n")
        new = self.commit("Change")
        # Against the original base the change is two commits away; against
        # the commit it actually sits on, it is fine.
        self.assertFails(self.verify(new), "exactly one commit directly on top of")
        self.assertPasses(self.verify(new, base=moved))

    def test_added_runtime_database_is_rejected(self) -> None:
        self.write("data/metrics.db", "binary-ish")
        self.assertFails(self.verify(self.commit("Add db")), "generated or credential-shaped")

    def test_deleting_a_committed_database_is_allowed(self) -> None:
        self.write("metrics.db", "junk")
        base = self.commit("Accidentally commit a database")
        (self.repo / "metrics.db").unlink()
        self.write(".gitignore", "*.db\n")
        self.assertPasses(self.verify(self.commit("Stop tracking the runtime database"), base=base))

    def test_dependency_directory_is_rejected(self) -> None:
        self.write("node_modules/left-pad/index.js", "module.exports = 1\n")
        self.assertFails(self.verify(self.commit("Vendor deps")), "generated or credential-shaped")

    def test_env_example_is_allowed_but_env_is_not(self) -> None:
        self.write(".env.example", "API_URL=http://localhost:8080\n")
        self.assertPasses(self.verify(self.commit("Document configuration")))

    def test_dot_env_is_rejected(self) -> None:
        self.write(".env", "API_URL=http://localhost:8080\n")
        self.assertFails(self.verify(self.commit("Add env")), "generated or credential-shaped")

    def test_credential_shape_in_added_line_is_rejected(self) -> None:
        fake_key = "AKIA" + "ABCDEFGHIJKLMNOP"  # assembled so this file holds no credential shape
        self.write("config.py", f"KEY = '{fake_key}'\n")
        self.assertFails(self.verify(self.commit("Add config")), "looks like a credential")

    def test_credential_shape_in_removed_line_is_fine(self) -> None:
        fake_key = "AKIA" + "ABCDEFGHIJKLMNOP"
        self.write("config.py", f"KEY = '{fake_key}'\n")
        base = self.commit("Leak")
        self.write("config.py", "import os\nKEY = os.environ['KEY']\n")
        self.assertPasses(self.verify(self.commit("Read the key from the environment"), base=base))

    def test_workflow_edit_needs_the_scope(self) -> None:
        self.write(".github/workflows/ci.yml", "name: ci\n")
        new = self.commit("Add CI")
        self.assertFails(self.verify(new, ALLOW_WORKFLOW_EDITS="false"), "workflow scope")
        self.assertPasses(self.verify(new, ALLOW_WORKFLOW_EDITS="true"))
        self.assertPasses(self.verify(new, ALLOW_WORKFLOW_EDITS="unknown"))

    def test_size_cap_ignores_lockfiles(self) -> None:
        self.write("uv.lock", "".join(f"line {i}\n" for i in range(500)))
        self.write("web/package-lock.json", "".join(f"{i}\n" for i in range(500)))
        self.write("app.py", "print('small change')\n")
        result = self.verify(self.commit("Bump dependencies"))
        self.assertPasses(result)
        self.assertIn("changed_lines=2", result.stdout)

    def test_size_cap_is_enforced(self) -> None:
        self.write("big.py", "".join(f"x{i} = {i}\n" for i in range(60)))
        self.assertFails(self.verify(self.commit("Big change")), "the cap is 50")

    def test_submodule_is_rejected(self) -> None:
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.base},vendor/lib")
        self.git("commit", "-q", "-m", "Add submodule", env={
            "GIT_AUTHOR_NAME": OWNER[0], "GIT_AUTHOR_EMAIL": OWNER[1],
            "GIT_COMMITTER_NAME": OWNER[0], "GIT_COMMITTER_EMAIL": OWNER[1],
        })
        self.assertFails(self.verify(self.git("rev-parse", "HEAD")), "submodule")


if __name__ == "__main__":
    unittest.main()
