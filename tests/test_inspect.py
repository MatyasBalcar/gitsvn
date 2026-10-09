"""Read-only inspection coverage using isolated SVN repositories."""

import io
import json
import os
import re
import shutil
import unittest
from unittest.mock import patch

from gitsvn.app import GitSvn
from gitsvn.cli import main
import test_gitsvn as fixtures


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"),
                     "svn and svnadmin must be on PATH")
class InspectIntegrationTests(unittest.TestCase):
    # Borrow the existing isolated fixture without inheriting its unrelated tests.
    setUp = fixtures.GitSvnIntegrationTests.setUp
    run_process = fixtures.GitSvnIntegrationTests.run_process
    cli = fixtures.GitSvnIntegrationTests.cli
    svn = fixtures.GitSvnIntegrationTests.svn
    write = fixtures.GitSvnIntegrationTests.write
    read = fixtures.GitSvnIntegrationTests.read
    snapshots = fixtures.GitSvnIntegrationTests.snapshots
    configure = fixtures.GitSvnIntegrationTests.configure
    new_branch = fixtures.GitSvnIntegrationTests.new_branch
    commit = fixtures.GitSvnIntegrationTests.commit
    tree_contents = fixtures.GitSvnIntegrationTests.tree_contents

    def inspect_unchanged(self, answers="q\n", success=True):
        before = {name: self.tree_contents(path) for name, path in (
            ("working copy", self.wc), ("snapshots", self.patches), ("state", self.state),
        )}
        result = self.cli("inspect", input=answers, success=success)
        for name, path in (("working copy", self.wc), ("snapshots", self.patches),
                           ("state", self.state)):
            self.assertEqual(self.tree_contents(path), before[name], name)
        self.assertNotIn("\x1b[", result.stdout)
        return result

    def file_choice(self, menu, path):
        for line in menu.splitlines():
            choice = re.match(r"\s*(\d+)\.\s", line)
            if choice and re.search(r"(?<![\w/])" + re.escape(path) + r"/?(?:\s|$)", line):
                return choice[1]
        self.fail(f"File is not present in the numbered menu: {path}\n{menu}")

    def test_inspect_uses_recorded_head_instead_of_recovery_or_pending_edits(self):
        self.configure(autosave=False)
        self.new_branch()
        self.write("story.txt", "selected saved version\n")
        selected = self.commit("selected commit message")
        self.write("story.txt", "later committed version\n")
        self.commit("later commit message")
        self.write("story.txt", "recovery autosave version\n")
        self.cli("revert", "18814", selected)
        self.assertGreater(self.snapshots("18814")[-1].stem, selected)
        self.write("story.txt", "pending working copy version\n")

        result = self.inspect_unchanged("1\nq\n")

        self.assertIn(selected, result.stdout)
        self.assertIn("-original", result.stdout)
        self.assertIn("+selected saved version", result.stdout)
        self.assertNotIn("later committed version", result.stdout)
        self.assertNotIn("recovery autosave version", result.stdout)
        self.assertNotIn("pending working copy version", result.stdout)

    def test_inspect_displays_hunks_and_nearby_context_without_whole_file(self):
        original = [f"line {number:02d}" for number in range(1, 31)]
        self.write("story.txt", "\n".join(original) + "\n")
        self.svn("commit", "-m", "long base file")
        self.new_branch()
        modified = original.copy()
        modified[14] = "changed line 15"
        self.write("story.txt", "\n".join(modified) + "\n")
        self.commit("middle of file")

        result = self.inspect_unchanged("1\nq\n")

        self.assertIn("@@", result.stdout)
        self.assertIn("-line 15", result.stdout)
        self.assertIn("+changed line 15", result.stdout)
        self.assertIn(" line 14", result.stdout)
        self.assertIn(" line 16", result.stdout)
        self.assertNotIn("line 01", result.stdout)
        self.assertNotIn("line 30", result.stdout)

    def test_property_payload_is_not_mistaken_for_another_file(self):
        self.new_branch()
        value = "++ value: thing\nIndex: phantom.txt\nProperty changes on: phantom.txt"
        self.svn("propset", "custom:note", value, "properties.txt")
        self.write("story.txt", "other file must stay separate\n")
        self.commit("property and text")
        menu = self.inspect_unchanged().stdout
        choice = self.file_choice(menu, "properties.txt")
        self.assertNotIn("phantom.txt", menu)

        result = self.inspect_unchanged(f"{choice}\nq\n")

        self.assertIn("custom:note", result.stdout)
        self.assertIn("+++ value: thing", result.stdout)
        self.assertIn("+Index: phantom.txt", result.stdout)
        self.assertNotIn("+other file must stay separate", result.stdout)

    def test_inspect_includes_embedded_properties_for_modified_empty_file(self):
        self.new_branch()
        self.write("story.txt", "")
        self.svn("propset", "custom:flag", "empty file property", "story.txt")
        snapshot = self.commit("truncate and set property")
        metadata = json.loads((self.patches / "18814" / f"{snapshot}.json")
                              .read_text(encoding="utf-8"))
        self.assertIn("story.txt", metadata["empty_properties"])

        result = self.inspect_unchanged("1\nq\n")

        self.assertIn("-original", result.stdout)
        self.assertIn("custom:flag", result.stdout)
        self.assertIn("+empty file property", result.stdout)
        self.assertEqual(result.stdout.count("+empty file property"), 1)

    def test_inspect_lists_empty_file_and_directory_from_snapshot_metadata(self):
        self.new_branch()
        self.write("empty.txt", "")
        (self.wc / "emptydir").mkdir()
        self.cli("add", "empty.txt", "emptydir")
        self.commit("empty paths")
        menu = self.inspect_unchanged().stdout

        for path in ("empty.txt", "emptydir"):
            with self.subTest(path=path):
                choice = self.file_choice(menu, path)
                result = self.inspect_unchanged(f"{choice}\nq\n")
                self.assertIn(path, result.stdout)
                self.assertNotIn("-original", result.stdout)

    def test_inspect_deleted_directory_children_and_legacy_patch_without_metadata(self):
        self.write("tracked/nested/file.txt", "deleted directory content\n")
        self.write("emptybase.txt", "")
        self.svn("add", "tracked", "emptybase.txt")
        self.svn("commit", "-m", "directory fixture")
        self.new_branch()
        self.svn("delete", "tracked", "emptybase.txt")
        snapshot = self.commit("delete directory and empty file")
        menu = self.inspect_unchanged().stdout
        self.file_choice(menu, "tracked")
        self.file_choice(menu, "emptybase.txt")
        child = "tracked/nested/file.txt"
        choice = self.file_choice(menu, child)
        result = self.inspect_unchanged(f"{choice}\nq\n")
        self.assertIn("-deleted directory content", result.stdout)

        # Historical snapshots may consist of only a patch/message pair.
        (self.patches / "18814" / f"{snapshot}.json").unlink()
        legacy_menu = self.inspect_unchanged().stdout
        choice = self.file_choice(legacy_menu, child)
        legacy = self.inspect_unchanged(f"{choice}\nq\n")
        self.assertIn("-deleted directory content", legacy.stdout)

    def test_cancelling_picker_or_reaching_eof_never_changes_files(self):
        self.new_branch()
        self.write("story.txt", "saved content\n")
        self.commit("saved commit")
        self.write("story.txt", "unsaved content\n")
        for answers in ("q\n", "\n", "", "1\n"):
            with self.subTest(answers=answers):
                result = self.inspect_unchanged(answers)
                self.assertIn("story.txt", result.stdout)
                self.assertEqual(self.read("story.txt"), "unsaved content\n")

    def test_interactive_picker_viewer_and_back_use_only_saved_snapshot(self):
        self.new_branch()
        self.write("story.txt", "saved interactive content\n")
        snapshot = self.commit("interactive inspection")
        self.write("story.txt", "pending interactive content\n")
        before = {name: self.tree_contents(path) for name, path in (
            ("working copy", self.wc), ("snapshots", self.patches), ("state", self.state),
        )}
        output = io.StringIO()
        keys = iter(["\r", "q", "\x1b"])
        with patch("gitsvn.terminal.picker_input") as console, \
                patch("gitsvn.cli.sys.stdout", output), \
                patch("gitsvn.terminal.shutil.get_terminal_size", return_value=os.terminal_size((160, 24))), \
                patch.object(GitSvn, "svn", side_effect=AssertionError("SVN query")), \
                patch.object(GitSvn, "verify_working_copy",
                             side_effect=AssertionError("SVN verification")):
            console.return_value.__enter__.return_value = keys.__next__
            code = main([
                "--svn-root", str(self.wc), "--patch-root", str(self.patches),
                "--state-dir", str(self.state), "inspect",
            ])

        self.assertEqual(code, 0)
        rendered = output.getvalue()
        self.assertEqual(rendered.count("Inspect 18814:"), 2)
        self.assertEqual(rendered.count("(current version)"), 2)
        self.assertIn(snapshot, rendered)
        self.assertIn("-original", rendered)
        self.assertIn("+saved interactive content", rendered)
        self.assertNotIn("pending interactive content", rendered)
        self.assertIn("\x1b[?1049h", rendered)
        self.assertIn("\x1b[?1049l", rendered)
        for name, path in (("working copy", self.wc), ("snapshots", self.patches),
                           ("state", self.state)):
            self.assertEqual(self.tree_contents(path), before[name], name)

    def test_branch_with_no_head_or_clean_head_has_no_diff_picker(self):
        no_head = self.inspect_unchanged("")
        self.assertIn("No saved head on branch trunk", no_head.stdout)
        self.new_branch()
        self.commit("clean current version")
        clean = self.inspect_unchanged("")
        self.assertIn("No changed files in current version.", clean.stdout)
        self.assertNotIn("number", clean.stdout.lower())

        state_path = self.state / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        del state["heads"]["18814"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertTrue(self.snapshots("18814"))
        unselected = self.inspect_unchanged("")
        self.assertIn("No saved head on branch 18814", unselected.stdout)
        self.assertNotIn("No changed files", unselected.stdout)

    def test_missing_or_invalid_recorded_head_fails_without_falling_back(self):
        self.new_branch()
        self.write("story.txt", "saved content\n")
        snapshot = self.commit("saved commit")
        state_path = self.state / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["heads"]["18814"] = "missing-snapshot"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        missing = self.inspect_unchanged("", success=False)
        self.assertIn("head", missing.stderr.lower())
        self.assertIn("missing-snapshot", missing.stderr)
        self.assertNotIn("+saved content", missing.stdout)

        state["heads"]["18814"] = snapshot
        state_path.write_text(json.dumps(state), encoding="utf-8")
        metadata_path = self.patches / "18814" / f"{snapshot}.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["entries"] = "invalid entries"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        invalid = self.inspect_unchanged("", success=False)
        self.assertIn("metadata", invalid.stderr.lower())
        self.assertNotIn("+saved content", invalid.stdout)


if __name__ == "__main__":
    unittest.main()
