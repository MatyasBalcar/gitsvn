"""Single-file restoration coverage using isolated, local SVN repositories."""

import io
import json
import shutil
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from gitsvn.app import GitSvn
from gitsvn.models import GitSvnError
import test_gitsvn as fixtures


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"),
                     "svn and svnadmin must be on PATH")
class RestoreIntegrationTests(unittest.TestCase):
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
    svn_status = fixtures.GitSvnIntegrationTests.svn_status
    tree_contents = fixtures.GitSvnIntegrationTests.tree_contents

    def app(self):
        return GitSvn(self.wc, self.patches, self.state)

    def workspace_contents(self):
        return {name: content for name, content in self.tree_contents(self.wc).items()
                if not name.replace("\\", "/").startswith(".svn/")}

    def status(self):
        return ET.canonicalize(self.svn("status", "--xml", "--no-ignore").stdout)

    def state_contents(self):
        return {name: content for name, content in self.tree_contents(self.state).items()
                if not name.replace("\\", "/").startswith("restores/")}

    def assert_history_unchanged(self, state_before, patches_before):
        self.assertEqual(self.state_contents(), state_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)

    def assert_recovery_contains(self, content):
        recovery = self.tree_contents(self.state / "restores")
        self.assertTrue(recovery, "Restore must keep a recovery copy outside branch history")
        self.assertTrue(any(content in value for value in recovery.values()), recovery.keys())

    def assert_rejected_without_changes(self, name):
        before = (self.workspace_contents(), self.status(),
                  self.tree_contents(self.state), self.tree_contents(self.patches))
        with self.assertRaises(GitSvnError):
            self.app().restore_file(name)
        self.assertEqual((self.workspace_contents(), self.status(),
                          self.tree_contents(self.state), self.tree_contents(self.patches)), before)

    def test_numbered_picker_restores_selected_saved_file_and_preserves_other_work(self):
        self.new_branch()
        self.write("story.txt", "saved selected file\n")
        self.write("removed.txt", "saved other file\n")
        head = self.commit("saved picker files")
        self.write("story.txt", "pending selected file\n")
        self.write("removed.txt", "pending other file\n")
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        result = self.cli("restore", input="2\n")

        self.assertLess(result.stdout.index("removed.txt"), result.stdout.index("story.txt"))
        self.assertEqual(self.read("story.txt"), "saved selected file\n")
        self.assertEqual(self.read("removed.txt"), "pending other file\n")
        self.assertEqual(self.app().state["heads"]["18814"], head)
        self.assert_history_unchanged(state_before, patches_before)
        self.assert_recovery_contains(b"pending selected file")

    def test_numbered_picker_cancel_eof_and_invalid_choices_do_not_write_without_head(self):
        self.app()
        self.write("story.txt", "pending work without a head\n")
        before = (self.workspace_contents(), self.status(),
                  self.tree_contents(self.state), self.tree_contents(self.patches))
        for answer, success in (("q\n", True), ("", True), ("\n", True),
                                ("invalid\n", False), ("0\n", False), ("2\n", False)):
            with self.subTest(answer=answer):
                result = self.cli("restore", input=answer, success=success)
                if success:
                    self.assertIn("File restore cancelled", result.stdout)
                else:
                    self.assertIn("Choose a file number shown in the list", result.stderr)
                self.assertEqual((self.workspace_contents(), self.status(),
                                  self.tree_contents(self.state), self.tree_contents(self.patches)), before)

    def test_picker_lists_status_files_and_excludes_directories_and_excluded_paths(self):
        self.write("shared/file.txt", "external fixture\n")
        self.svn("add", "shared")
        self.svn("propset", "svn:externals", "^/shared external", ".")
        self.svn("propset", "svn:ignore", "*.tmp", ".")
        self.svn("commit", "-m", "picker exclusion fixture")
        self.svn("update")
        self.new_branch()
        self.write("story.txt", "modified file\n")
        self.write("added.txt", "added file\n")
        self.svn("add", "added.txt")
        self.svn("delete", "removed.txt")
        self.svn("propset", "custom:flag", "property-only file", "properties.txt")
        self.write("ignored.txt", "ignore-on-commit file\n")
        self.svn("changelist", "ignore-on-commit", "ignored.txt")
        self.write("loose.txt", "unversioned file\n")
        self.write("loose.tmp", "svn-ignored file\n")
        self.write("external/file.txt", "external working-copy edit\n")
        (self.wc / "empty-directory").mkdir()
        self.svn("add", "empty-directory")
        before = (self.workspace_contents(), self.status(),
                  self.tree_contents(self.state), self.tree_contents(self.patches))

        with patch("gitsvn.restoration.pick_item", return_value=None) as picker:
            self.app().restore_file()

        self.assertEqual(picker.call_args.args[0],
                         ["added.txt", "properties.txt", "removed.txt", "story.txt"])
        self.assertEqual((self.workspace_contents(), self.status(),
                          self.tree_contents(self.state), self.tree_contents(self.patches)), before)

    def test_clean_picker_without_head_reports_empty_list_without_menu_or_writes(self):
        app = self.app()
        before = (self.workspace_contents(), self.status(),
                  self.tree_contents(self.state), self.tree_contents(self.patches))
        output = io.StringIO()

        with patch("gitsvn.restoration.pick_item") as picker, \
                patch("gitsvn.cli.sys.stdout", output):
            app.restore_file()

        picker.assert_not_called()
        self.assertIn("No tracked file changes to restore", output.getvalue())
        self.assertEqual((self.workspace_contents(), self.status(),
                          self.tree_contents(self.state), self.tree_contents(self.patches)), before)

    def test_directory_only_changes_report_empty_file_list_without_menu_or_writes(self):
        self.new_branch()
        (self.wc / "empty-directory").mkdir()
        self.svn("add", "empty-directory")
        self.svn("propset", "custom:directory", "pending directory property", ".")
        before = (self.workspace_contents(), self.status(),
                  self.tree_contents(self.state), self.tree_contents(self.patches))
        output = io.StringIO()

        with patch("gitsvn.restoration.pick_item") as picker, \
                patch("gitsvn.cli.sys.stdout", output):
            self.app().restore_file()

        picker.assert_not_called()
        self.assertIn("No tracked file changes to restore", output.getvalue())
        self.assertEqual((self.workspace_contents(), self.status(),
                          self.tree_contents(self.state), self.tree_contents(self.patches)), before)

    def test_arrow_picker_selects_one_file_and_preserves_other_work(self):
        self.new_branch()
        self.write("story.txt", "saved arrow selection\n")
        self.commit("saved arrow file")
        self.write("story.txt", "pending arrow selection\n")
        self.write("removed.txt", "pending other file\n")
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)
        output = io.StringIO()

        with patch("gitsvn.terminal.picker_input") as console, \
                patch("gitsvn.cli.sys.stdout", output):
            console.return_value.__enter__.return_value = iter(["\xe0", "P", "\r"]).__next__
            self.app().restore_file()

        self.assertEqual(self.read("story.txt"), "saved arrow selection\n")
        self.assertEqual(self.read("removed.txt"), "pending other file\n")
        self.assertIn("Up/Down, Enter", output.getvalue())
        self.assert_history_unchanged(state_before, patches_before)
        self.assert_recovery_contains(b"pending arrow selection")

    def test_arrow_picker_escape_cancels_without_head_or_writes(self):
        app = self.app()
        self.write("story.txt", "pending work without a head\n")
        before = (self.workspace_contents(), self.status(),
                  self.tree_contents(self.state), self.tree_contents(self.patches))
        output = io.StringIO()

        with patch("gitsvn.terminal.picker_input") as console, \
                patch("gitsvn.cli.sys.stdout", output):
            console.return_value.__enter__.return_value = iter(["\x1b"]).__next__
            app.restore_file()

        self.assertIn("File restore cancelled", output.getvalue())
        self.assertEqual((self.workspace_contents(), self.status(),
                          self.tree_contents(self.state), self.tree_contents(self.patches)), before)

    def test_restore_saved_file_preserves_unrelated_bytes_status_properties_and_history(self):
        self.new_branch()
        saved = b"saved text\r\nsecond line\r\n"
        (self.wc / "story.txt").write_bytes(saved)
        self.write("removed.txt", "saved unrelated branch edit\n")
        self.svn("propset", "custom:note", "saved unrelated property", "properties.txt")
        self.commit("saved target")
        self.write("story.txt", "discarded target edit\n")
        self.write("removed.txt", "unrelated pending edit\n")
        self.write("later.txt", "unrelated addition\n")
        self.svn("add", "later.txt")
        self.write("ignored.txt", "ignored pending edit\n")
        self.svn("changelist", "ignore-on-commit", "ignored.txt")
        self.svn("propset", "custom:note", "unrelated property", "properties.txt")
        self.write("loose.txt", "unversioned bytes\n")
        (self.wc / "binary.bin").write_bytes(b"\x00\x01\xffunrelated binary addition")
        self.svn("add", "binary.bin")
        self.svn("propset", "svn:mime-type", "application/octet-stream", "binary.bin")
        files_before = self.workspace_contents()
        status_before = self.status()
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        result = self.cli("restore", "story.txt")

        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.wc / "story.txt").read_bytes(), saved)
        self.assertEqual({name: value for name, value in self.workspace_contents().items()
                          if name != "story.txt"},
                         {name: value for name, value in files_before.items() if name != "story.txt"})
        self.assertEqual(self.status(), status_before)
        self.assertEqual(self.svn("propget", "custom:note", "properties.txt").stdout.strip(),
                         "unrelated property")
        self.assert_history_unchanged(state_before, patches_before)
        self.assert_recovery_contains(b"discarded target edit")

    def test_restore_uses_saved_historical_head_despite_newer_autosave(self):
        self.new_branch()
        self.write("story.txt", "historical saved file\n")
        first = self.commit("historical head")
        self.write("story.txt", "newer committed file\n")
        self.commit("newer snapshot")
        self.cli("revert", "18814", first)
        self.write("story.txt", "unsaved after historical revert\n")
        self.assertNotEqual(self.app().history("18814")[0].stem, first)
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        self.app().restore_file("story.txt")

        self.assertEqual(self.read("story.txt"), "historical saved file\n")
        self.assertEqual(self.app().state["heads"]["18814"], first)
        self.assert_history_unchanged(state_before, patches_before)

    def test_file_absent_in_saved_patch_returns_to_svn_base(self):
        self.new_branch()
        self.write("removed.txt", "saved unrelated branch file\n")
        self.commit("head omits target")
        self.write("story.txt", "pending target edit\n")
        self.svn("propset", "custom:pending", "pending property", "story.txt")
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        self.app().restore_file("story.txt")

        self.assertEqual(self.read("story.txt"), "original\n")
        self.assertNotIn("story.txt", self.svn_status())
        self.assertEqual(self.read("removed.txt"), "saved unrelated branch file\n")
        self.assert_history_unchanged(state_before, patches_before)
        self.assert_recovery_contains(b"pending target edit")

    def test_pending_added_file_absent_in_head_is_removed(self):
        self.new_branch()
        self.write("pending.txt", "discarded pending addition\n")
        self.svn("add", "pending.txt")
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        self.app().restore_file("pending.txt")

        self.assertFalse((self.wc / "pending.txt").exists())
        self.assertNotIn("pending.txt", self.svn_status())
        self.assert_history_unchanged(state_before, patches_before)
        self.assert_recovery_contains(b"discarded pending addition")

    def test_saved_added_file_restores_content_and_added_schedule(self):
        self.new_branch()
        self.write("new file.txt", "saved addition\n")
        self.svn("add", "new file.txt")
        self.commit("saved addition")
        self.write("new file.txt", "changed addition\n")
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        self.app().restore_file("new file.txt")

        self.assertEqual(self.read("new file.txt"), "saved addition\n")
        self.assertEqual(self.svn_status()["new file.txt"], "added")
        self.assert_history_unchanged(state_before, patches_before)

    def test_saved_deleted_file_restores_deleted_schedule(self):
        self.new_branch()
        self.svn("delete", "removed.txt")
        self.commit("saved deletion")
        self.svn("revert", "removed.txt")
        self.write("removed.txt", "pending resurrection\n")
        self.write("story.txt", "unrelated edit remains\n")
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        self.app().restore_file("removed.txt")

        self.assertFalse((self.wc / "removed.txt").exists())
        self.assertEqual(self.svn_status()["removed.txt"], "deleted")
        self.assertEqual(self.read("story.txt"), "unrelated edit remains\n")
        self.assert_history_unchanged(state_before, patches_before)
        self.assert_recovery_contains(b"pending resurrection")

    def test_saved_empty_modified_file_restores_empty_contents_and_properties(self):
        self.new_branch()
        self.write("story.txt", "")
        self.svn("propset", "custom:flag", "saved empty property", "story.txt")
        self.commit("empty target and property")
        self.write("story.txt", "pending nonempty edit\n")
        self.svn("propset", "custom:flag", "pending property", "story.txt")
        self.svn("propset", "custom:later", "discarded property", "story.txt")
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        self.app().restore_file("story.txt")

        self.assertEqual((self.wc / "story.txt").read_bytes(), b"")
        self.assertEqual(self.svn_status()["story.txt"], "modified")
        self.assertEqual(self.svn("propget", "custom:flag", "story.txt").stdout.strip(),
                         "saved empty property")
        self.svn("propget", "custom:later", "story.txt", success=False)
        self.assert_history_unchanged(state_before, patches_before)

    def test_saved_empty_added_file_restores_empty_contents_and_added_schedule(self):
        self.new_branch()
        self.write("empty.txt", "")
        self.svn("add", "empty.txt")
        self.commit("empty addition")
        self.write("empty.txt", "pending populated addition\n")

        self.app().restore_file("empty.txt")

        self.assertEqual((self.wc / "empty.txt").read_bytes(), b"")
        self.assertEqual(self.svn_status()["empty.txt"], "added")

    def test_saved_property_only_change_restores_properties_without_changing_text(self):
        self.new_branch()
        self.svn("propset", "custom:flag", "saved property", "properties.txt")
        self.commit("property only head")
        self.write("properties.txt", "pending text edit\n")
        self.svn("propset", "custom:flag", "pending property", "properties.txt")

        self.app().restore_file("properties.txt")

        self.assertEqual(self.read("properties.txt"), "property target\n")
        self.assertEqual(self.svn("propget", "custom:flag", "properties.txt").stdout.strip(),
                         "saved property")

    def test_header_like_hunk_payload_does_not_include_another_file(self):
        self.new_branch()
        saved = ("++ removed.txt\t(working copy)\nIndex: removed.txt\n"
                 "Property changes on: properties.txt\n@@ -1 +1 @@\n")
        self.write("story.txt", saved)
        self.write("removed.txt", "saved unrelated file\n")
        self.commit("header-like payload and unrelated file")
        self.write("story.txt", "pending target edit\n")
        self.write("removed.txt", "pending unrelated file\n")
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        self.app().restore_file("story.txt")

        self.assertEqual(self.read("story.txt"), saved)
        self.assertEqual(self.read("removed.txt"), "pending unrelated file\n")
        self.assert_history_unchanged(state_before, patches_before)

    def test_apply_failure_rolls_back_target_and_preserves_other_files_and_history(self):
        self.new_branch()
        self.write("story.txt", "saved target\n")
        self.svn("propset", "custom:flag", "saved property", "story.txt")
        self.commit("saved target and property")
        self.write("story.txt", "pending target before failed restore\n")
        self.svn("propset", "custom:flag", "pending property", "story.txt")
        self.write("removed.txt", "unrelated pending edit\n")
        before = self.workspace_contents(), self.status()
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)
        app = self.app()
        apply_snapshot = app.apply_snapshot

        def fail_after_apply(path, snapshot):
            apply_snapshot(path, snapshot)
            if path.name == "restore.patch":
                (self.wc / "story.txt.svnpatch.rej").write_bytes(b"injected reject")
                raise GitSvnError("Injected application failure")

        with patch.object(app, "apply_snapshot", side_effect=fail_after_apply):
            with self.assertRaisesRegex(GitSvnError, "previous changes restored"):
                app.restore_file("story.txt")

        self.assertEqual((self.workspace_contents(), self.status()), before)
        self.assertEqual(self.svn("propget", "custom:flag", "story.txt").stdout.strip(),
                         "pending property")
        self.assert_history_unchanged(state_before, patches_before)
        self.assert_recovery_contains(b"pending target before failed restore")

    def test_absolute_target_inside_checkout_restores_from_another_directory(self):
        self.new_branch()
        self.write("story.txt", "saved target\n")
        self.commit("saved target")
        self.write("story.txt", "pending target edit\n")
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        self.cli("restore", str(self.wc / "story.txt"))

        self.assertEqual(self.read("story.txt"), "saved target\n")
        self.assert_history_unchanged(state_before, patches_before)

    def test_restore_file_inside_saved_added_directory_preserves_parent_and_sibling(self):
        self.new_branch()
        self.write("newdir/target.txt", "saved nested target\n")
        self.write("newdir/sibling.txt", "saved sibling\n")
        self.svn("add", "newdir")
        self.svn("propset", "custom:directory", "saved directory property", "newdir")
        self.commit("saved added directory")
        self.write("newdir/target.txt", "pending nested target\n")
        self.write("newdir/sibling.txt", "pending sibling remains\n")
        self.svn("propset", "custom:directory", "pending directory property", "newdir")
        status_before = self.status()
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        self.app().restore_file("newdir\\target.txt")

        self.assertEqual(self.read("newdir/target.txt"), "saved nested target\n")
        self.assertEqual(self.read("newdir/sibling.txt"), "pending sibling remains\n")
        self.assertEqual(self.svn("propget", "custom:directory", "newdir").stdout.strip(),
                         "pending directory property")
        self.assertEqual(self.status(), status_before)
        self.assert_history_unchanged(state_before, patches_before)

    def test_manually_deleted_tracked_file_restores_saved_head_text_and_properties(self):
        self.new_branch()
        self.write("story.txt", "saved target\n")
        self.svn("propset", "custom:flag", "saved property", "story.txt")
        self.commit("saved target and property")
        self.svn("propset", "custom:flag", "pending property", "story.txt")
        (self.wc / "story.txt").unlink()
        self.write("removed.txt", "unrelated pending edit\n")
        self.assertEqual(self.svn_status()["story.txt"], "missing")
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)

        self.app().restore_file("story.txt")

        self.assertEqual(self.read("story.txt"), "saved target\n")
        self.assertEqual(self.svn("propget", "custom:flag", "story.txt").stdout.strip(),
                         "saved property")
        self.assertEqual(self.read("removed.txt"), "unrelated pending edit\n")
        self.assert_history_unchanged(state_before, patches_before)

    def test_manually_deleted_file_failure_rolls_back_missing_state_and_pending_properties(self):
        self.new_branch()
        self.write("story.txt", "saved target\n")
        self.svn("propset", "custom:flag", "saved property", "story.txt")
        self.commit("saved target and property")
        self.svn("propset", "custom:flag", "pending property", "story.txt")
        (self.wc / "story.txt").unlink()
        self.write("removed.txt", "unrelated pending edit\n")
        before = self.workspace_contents(), self.status()
        state_before, patches_before = self.state_contents(), self.tree_contents(self.patches)
        app = self.app()
        apply_snapshot = app.apply_snapshot

        def fail_after_apply(path, snapshot):
            apply_snapshot(path, snapshot)
            if path.name == "restore.patch":
                raise GitSvnError("Injected application failure")

        with patch.object(app, "apply_snapshot", side_effect=fail_after_apply):
            with self.assertRaisesRegex(GitSvnError, "previous changes restored"):
                app.restore_file("story.txt")

        self.assertEqual((self.workspace_contents(), self.status()), before)
        self.assertEqual(self.svn("propget", "custom:flag", "story.txt").stdout.strip(),
                         "pending property")
        self.assert_history_unchanged(state_before, patches_before)

    def test_missing_head_is_rejected_without_changes(self):
        self.new_branch()
        self.write("story.txt", "pending edit\n")
        state = json.loads((self.state / "state.json").read_text(encoding="utf-8"))
        state["heads"].pop("18814")
        (self.state / "state.json").write_text(json.dumps(state), encoding="utf-8")

        self.assert_rejected_without_changes("story.txt")

    def test_nonexistent_or_unsafe_head_is_rejected_without_changes(self):
        self.new_branch()
        self.write("story.txt", "pending edit\n")
        state = json.loads((self.state / "state.json").read_text(encoding="utf-8"))
        for head in ("missing-snapshot", "../../outside"):
            with self.subTest(head=head):
                state["heads"]["18814"] = head
                (self.state / "state.json").write_text(json.dumps(state), encoding="utf-8")
                self.assert_rejected_without_changes("story.txt")

    def test_invalid_snapshot_metadata_is_rejected_without_changes(self):
        self.new_branch()
        self.write("story.txt", "saved target\n")
        head = self.commit("saved target")
        metadata_path = self.patches / "18814" / (head + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["entries"][0]["path"] = "../outside.txt"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        self.write("story.txt", "pending target edit\n")

        self.assert_rejected_without_changes("story.txt")

    def test_ignored_unversioned_directory_and_unsafe_targets_are_rejected_without_changes(self):
        self.new_branch()
        self.write("ignored.txt", "ignored target edit\n")
        self.svn("changelist", "ignore-on-commit", "ignored.txt")
        self.write("loose.txt", "unversioned target\n")
        for name in ("ignored.txt", "loose.txt", ".", "../outside.txt", ".svn/wc.db",
                     str(self.root / "outside.txt")):
            with self.subTest(name=name):
                self.assert_rejected_without_changes(name)


if __name__ == "__main__":
    unittest.main()
