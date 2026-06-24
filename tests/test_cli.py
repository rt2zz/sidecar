import tempfile
import unittest
from pathlib import Path

from sidecar.cli import (
    SidecarConfig,
    ensure_gitignore_entry,
    expand_inbox,
    file_label,
    fork_path,
    read_config,
    write_config,
)


class ConfigTests(unittest.TestCase):
    def test_round_trip_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".sidecar"
            write_config(
                path,
                SidecarConfig(
                    remote="git@github.com:org/repo-sidecar.git",
                    path="metadata",
                    branch="main",
                    inbox="sidecar-inbox/{user}/{host}",
                ),
            )
            config = read_config(path)

        self.assertEqual(config.remote, "git@github.com:org/repo-sidecar.git")
        self.assertEqual(config.path, "metadata")
        self.assertEqual(config.branch, "main")
        self.assertEqual(config.inbox, "sidecar-inbox/{user}/{host}")

    def test_expand_inbox_has_prefix(self):
        config = SidecarConfig(remote="x")
        self.assertTrue(expand_inbox(config).startswith("sidecar-inbox/"))


class ForkPathTests(unittest.TestCase):
    def test_fork_path_keeps_extension(self):
        self.assertEqual(
            fork_path("notes/plan.md", "sidecar-inbox/zack/mbp", "abcdef123"),
            "notes/plan.conflict.sidecar-inbox-zack-mbp.abcdef1.md",
        )

    def test_fork_path_without_extension(self):
        self.assertEqual(
            fork_path("TODO", "main", "abcdef123"),
            "TODO.conflict.main.abcdef1",
        )

    def test_file_label_flattens_branch(self):
        self.assertEqual(file_label("sidecar-inbox/zack/mbp"), "sidecar-inbox-zack-mbp")


class GitignoreTests(unittest.TestCase):
    def test_gitignore_entry_is_root_anchored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".gitignore"
            ensure_gitignore_entry(path, "sidecar")
            self.assertEqual(path.read_text(encoding="utf-8"), "/sidecar/\n")


if __name__ == "__main__":
    unittest.main()
