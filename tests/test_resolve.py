"""Conflict resolution coverage using isolated, local SVN repositories."""

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
class ResolveIntegrationTests(unittest.TestCase):
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

    def upstream(self):
        checkout = self.root / "upstream"
        self.run_process(["svn", "checkout", self.repo_url, str(checkout)])
        return checkout

    def status_for(self, name):
        document = ET.fromstring(self.svn("status", "--xml").stdout)
        return next((entry.find("wc-status") for entry in document.findall(".//entry")
                     if entry.get("path", "").replace("\\", "/") == name), None)

    def make_text_conflict(self, name="story.txt", property_conflict=False,
                           second_property_conflict=False):
        base = [f"line {index:02d}\n" for index in range(30)]
        base[1], base[15], base[28] = "local base\n", "shared base\n", "incoming base\n"
        self.write(name, "".join(base))
        if name != "story.txt":
            self.svn("add", "--", name + "@" if "@" in name else name)
        if property_conflict:
            self.svn("propset", "custom:flag", "base property", "--", name)
            if second_property_conflict:
                self.svn("propset", "custom:second", "second base", "--", name)
        self.svn("commit", "-m", "conflict base")
        self.new_branch()
        upstream = self.upstream()
        incoming, local = base.copy(), base.copy()
        incoming[15], incoming[28] = "shared incoming\n", "incoming change\n"
        local[1], local[15] = "local change\n", "shared local\n"
        (upstream / name).write_text("".join(incoming), encoding="utf-8")
        if property_conflict:
            self.run_process(["svn", "propset", "custom:flag", "incoming property",
                              "--", name], cwd=upstream)
            self.svn("propset", "custom:flag", "local property", "--", name)
            if second_property_conflict:
                self.run_process(["svn", "propset", "custom:second", "second incoming",
                                  "--", name], cwd=upstream)
                self.svn("propset", "custom:second", "second local", "--", name)
        self.run_process(["svn", "commit", "-m", "incoming changes"], cwd=upstream)
        self.write(name, "".join(local))
        self.svn("update", "--accept", "postpone")
        self.assertEqual(self.status_for(name).get("item"), "conflicted")
        if property_conflict:
            self.assertEqual(self.status_for(name).get("props"), "conflicted")
        return base, local, incoming

    def assert_history_unchanged(self, state_before, patches_before):
        state_after = {path: content for path, content in self.tree_contents(self.state).items()
                       if not path.replace("\\", "/").startswith("resolutions/")}
        self.assertEqual(state_after, state_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)

    def resolve_strategy(self, strategy):
        base, local, incoming = self.make_text_conflict()
        self.write("removed.txt", "unrelated pending edit\n")
        conflict_before = (self.wc / "story.txt").read_bytes()
        artifacts_before = {path.name: path.read_bytes() for path in self.wc.glob("story.txt.*")}
        state_before, patches_before = self.tree_contents(self.state), self.tree_contents(self.patches)

        result = self.cli("resolve", "story.txt", "--accept", strategy, input="2\n")

        if strategy == "mine-full":
            expected = local
        elif strategy == "theirs-full":
            expected = incoming
        else:
            expected = incoming.copy()
            expected[1] = local[1]
            if strategy == "mine-conflict":
                expected[15] = local[15]
            elif strategy == "mine-first":
                expected[15] = local[15] + incoming[15]
            elif strategy == "theirs-first":
                expected[15] = incoming[15] + local[15]
        self.assertEqual(self.read("story.txt"), "".join(expected))
        status = self.status_for("story.txt")
        self.assertTrue(status is None or status.get("item") != "conflicted")
        self.assertEqual(self.read("removed.txt"), "unrelated pending edit\n")
        self.assert_history_unchanged(state_before, patches_before)
        backups = self.tree_contents(self.state / "resolutions")
        self.assertTrue(backups, result.stdout)
        self.assertIn(conflict_before, backups.values())
        for name, content in artifacts_before.items():
            self.assertIn(content, backups.values(), name)
        manifests = [json.loads(content) for path, content in backups.items()
                     if path.endswith(".json")]
        self.assertTrue(manifests)
        self.assertFalse(list(self.wc.glob("story.txt.*")))

    def test_mine_conflict_keeps_nonconflicting_incoming_changes(self):
        self.resolve_strategy("mine-conflict")

    def test_theirs_conflict_keeps_nonconflicting_local_changes(self):
        self.resolve_strategy("theirs-conflict")

    def test_mine_first_keeps_both_conflicting_sections_in_local_incoming_order(self):
        self.resolve_strategy("mine-first")

    def test_theirs_first_keeps_both_conflicting_sections_in_incoming_local_order(self):
        self.resolve_strategy("theirs-first")

    def test_mine_full_uses_entire_pre_update_local_file(self):
        self.resolve_strategy("mine-full")

    def test_theirs_full_uses_entire_incoming_file(self):
        self.resolve_strategy("theirs-full")

    def test_conflict_strategy_preserves_edits_made_after_update(self):
        _, local, incoming = self.make_text_conflict()
        self.write("story.txt", self.read("story.txt").replace("line 04\n", "manual after update\n"))

        self.cli("resolve", "story.txt", "--accept", "mine-conflict", input="2\n")

        expected = incoming.copy()
        expected[1], expected[4], expected[15] = local[1], "manual after update\n", local[15]
        self.assertEqual(self.read("story.txt"), "".join(expected))

    def test_stale_proposal_refuses_to_overwrite_new_edits_without_creating_backup(self):
        self.make_text_conflict()
        app = GitSvn(self.wc, self.patches, self.state)
        conflict = app.load_conflict(app.conflicted_entries()[0])
        resolution = app.propose_resolution(conflict, "mine-conflict")
        self.write("story.txt", "new edits after review\n")
        before, state_before = self.tree_contents(self.wc), self.tree_contents(self.state)

        with self.assertRaisesRegex(GitSvnError, "changed after the preview"):
            app.apply_resolution(conflict, resolution)

        self.assertEqual(self.tree_contents(self.wc), before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_svn_resolution_failure_retains_original_recovery_copies(self):
        self.make_text_conflict()
        app = GitSvn(self.wc, self.patches, self.state)
        conflict = app.load_conflict(app.conflicted_entries()[0])
        resolution = app.propose_resolution(conflict, "mine-conflict")
        state_before, patches_before = self.tree_contents(self.state), self.tree_contents(self.patches)
        svn = app.svn

        def fail_resolve(*arguments):
            if arguments[0] == "resolve":
                raise GitSvnError("simulated SVN resolution failure")
            return svn(*arguments)

        with patch.object(app, "svn", side_effect=fail_resolve), \
                self.assertRaisesRegex(GitSvnError, "Recovery copies:"):
            app.apply_resolution(conflict, resolution)

        self.assertIn(conflict.contents, self.tree_contents(self.state / "resolutions").values())
        for data in conflict.artifacts.values():
            self.assertIn(data, self.tree_contents(self.state / "resolutions").values())
        self.assert_history_unchanged(state_before, patches_before)
        self.assertEqual(self.status_for("story.txt").get("item"), "conflicted")

    def resolve_binary(self, method, expected):
        name = "binary.dat"
        (self.wc / name).write_bytes(b"\x00base\xff")
        self.svn("add", name)
        self.svn("propset", "svn:mime-type", "application/octet-stream", name)
        self.svn("commit", "-m", "binary fixture")
        self.new_branch()
        upstream = self.upstream()
        (upstream / name).write_bytes(b"\x00incoming\xff")
        self.run_process(["svn", "commit", "-m", "incoming binary"], cwd=upstream)
        (self.wc / name).write_bytes(b"\x00local\xff")
        self.svn("update", "--accept", "postpone")

        result = self.cli("resolve", name, "--accept", method, input="2\n")

        self.assertEqual((self.wc / name).read_bytes(), expected)
        status = self.status_for(name)
        self.assertTrue(status is None or status.get("item") != "conflicted")
        self.assertIn("SHA256", result.stdout)
        self.assertIn(b"\x00local\xff", self.tree_contents(self.state / "resolutions").values())

    def test_binary_mine_full_uses_current_local_file_when_svn_omits_mine_copy(self):
        self.resolve_binary("mine-full", b"\x00local\xff")

    def test_binary_theirs_full_uses_incoming_revision_copy(self):
        self.resolve_binary("theirs-full", b"\x00incoming\xff")

    def test_working_refuses_markers_then_accepts_manual_merge_and_backs_it_up(self):
        self.make_text_conflict()
        before = self.tree_contents(self.wc)
        state_before = self.tree_contents(self.state)
        patches_before = self.tree_contents(self.patches)
        rejected = self.cli("resolve", "story.txt", "--accept", "working", success=False)
        self.assertIn("marker", rejected.stderr.lower())
        self.assertEqual(self.tree_contents(self.wc), before)
        self.assertEqual(self.tree_contents(self.state), state_before)

        self.write("story.txt", "manually combined local and incoming edits\n")
        manual_bytes = (self.wc / "story.txt").read_bytes()
        self.cli("resolve", "story.txt", "--accept", "working", input="2\n")

        self.assertEqual((self.wc / "story.txt").read_bytes(), manual_bytes)
        self.assertNotEqual(self.status_for("story.txt").get("item"), "conflicted")
        self.assertIn(manual_bytes, self.tree_contents(self.state / "resolutions").values())
        self.assert_history_unchanged(state_before, patches_before)

    def test_mixed_text_and_property_conflict_is_resolved_together(self):
        self.make_text_conflict(property_conflict=True)
        property_reject = next(self.wc.glob("story.txt*.prej")).read_bytes()
        self.cli("resolve", "story.txt", "--accept", "mine-conflict", input="2\n")

        self.assertEqual(self.svn("propget", "custom:flag", "story.txt").stdout.strip(),
                         "local property")
        status = self.status_for("story.txt")
        self.assertNotEqual(status.get("item"), "conflicted")
        self.assertNotEqual(status.get("props"), "conflicted")
        self.assertIn(property_reject, self.tree_contents(self.state / "resolutions").values())

    def test_incoming_mixed_resolution_preserves_unrelated_pending_properties(self):
        self.make_text_conflict(property_conflict=True)
        self.svn("propset", "custom:unrelated", "pending property", "story.txt")
        self.write("story.txt", self.read("story.txt").replace("line 04\n", "manual after update\n"))

        self.cli("resolve", "story.txt", "--accept", "theirs-conflict", input="2\n")

        self.assertEqual(self.svn("propget", "custom:flag", "story.txt").stdout.strip(),
                         "incoming property")
        self.assertEqual(self.svn("propget", "custom:unrelated", "story.txt").stdout.strip(),
                         "pending property")
        self.assertIn("local change", self.read("story.txt"))
        self.assertIn("manual after update", self.read("story.txt"))
        self.assertIn("shared incoming", self.read("story.txt"))
        self.assertNotEqual(self.status_for("story.txt").get("props"), "conflicted")

    def test_incoming_mixed_resolution_selects_two_conflicted_properties(self):
        self.make_text_conflict(property_conflict=True, second_property_conflict=True)
        self.svn("propset", "custom:unrelated", "pending property", "story.txt")
        self.write("story.txt", self.read("story.txt").replace("line 04\n", "manual after update\n"))

        self.cli("resolve", "story.txt", "--accept", "theirs-conflict", input="2\n")

        for name, expected in (("custom:flag", "incoming property"),
                               ("custom:second", "second incoming"),
                               ("custom:unrelated", "pending property")):
            with self.subTest(property=name):
                self.assertEqual(self.svn("propget", name, "story.txt").stdout.strip(), expected)
        self.assertIn("local change", self.read("story.txt"))
        self.assertIn("manual after update", self.read("story.txt"))
        self.assertIn("shared incoming", self.read("story.txt"))
        self.assertNotEqual(self.status_for("story.txt").get("props"), "conflicted")

    def test_property_only_working_resolution_preserves_manually_chosen_property(self):
        self.svn("propset", "custom:flag", "base property", "properties.txt")
        self.svn("commit", "-m", "property base")
        self.new_branch()
        upstream = self.upstream()
        self.svn("propset", "custom:flag", "local property", "properties.txt")
        self.run_process(["svn", "propset", "custom:flag", "incoming property",
                          "properties.txt"], cwd=upstream)
        self.run_process(["svn", "commit", "-m", "incoming property"], cwd=upstream)
        self.svn("update", "--accept", "postpone")
        self.svn("propset", "custom:flag", "manual property", "properties.txt")

        self.cli("resolve", "properties.txt", "--accept", "working", input="2\n")

        self.assertEqual(self.svn("propget", "custom:flag", "properties.txt").stdout.strip(),
                         "manual property")
        self.assertNotEqual(self.status_for("properties.txt").get("props"), "conflicted")

    def test_resolving_directory_properties_does_not_resolve_conflicted_children(self):
        self.write("folder/child.txt", "child base\n")
        self.svn("add", "folder")
        self.svn("propset", "custom:flag", "base property", "folder")
        self.svn("commit", "-m", "directory fixture")
        self.new_branch()
        upstream = self.upstream()
        self.write("folder/child.txt", "local child\n")
        self.svn("propset", "custom:flag", "local property", "folder")
        (upstream / "folder" / "child.txt").write_text("incoming child\n", encoding="utf-8")
        self.run_process(["svn", "propset", "custom:flag", "incoming property", "folder"],
                         cwd=upstream)
        self.run_process(["svn", "commit", "-m", "incoming directory changes"], cwd=upstream)
        self.svn("update", "--accept", "postpone")
        child_before = (self.wc / "folder" / "child.txt").read_bytes()

        self.cli("resolve", "folder", "--accept", "working", input="2\n")

        self.assertNotEqual(self.status_for("folder").get("props"), "conflicted")
        self.assertEqual(self.svn("propget", "custom:flag", "folder").stdout.strip(),
                         "local property")
        self.assertEqual((self.wc / "folder" / "child.txt").read_bytes(), child_before)
        self.assertEqual(self.status_for("folder/child.txt").get("item"), "conflicted")

    def test_no_conflicts_and_cancelled_or_previewed_choices_are_read_only(self):
        self.new_branch()
        no_conflicts = self.cli("resolve", input="")
        self.assertIn("no", no_conflicts.stdout.lower())
        self.assertIn("conflict", no_conflicts.stdout.lower())
        # The rest needs an actual update conflict, without recreating the branch.
        self.write("story.txt", "local edit\n")
        upstream = self.upstream()
        (upstream / "story.txt").write_text("incoming edit\n", encoding="utf-8")
        self.run_process(["svn", "commit", "-m", "incoming conflict"], cwd=upstream)
        self.svn("update", "--accept", "postpone")
        for arguments, answers in (((), "q\n"), ((), ""),
                                   (("story.txt",), "q\n"),
                                   (("story.txt",), ""),
                                   (("story.txt",), "1\nq\n")):
            with self.subTest(arguments=arguments, answers=answers):
                before = self.tree_contents(self.wc)
                state_before = self.tree_contents(self.state)
                patches_before = self.tree_contents(self.patches)
                result = self.cli("resolve", *arguments, input=answers)
                self.assertEqual(self.tree_contents(self.wc), before)
                self.assertEqual(self.tree_contents(self.state), state_before)
                self.assertEqual(self.tree_contents(self.patches), patches_before)
                self.assertNotIn("\x1b[", result.stdout)

    def test_proposal_requires_explicit_apply_confirmation(self):
        self.make_text_conflict()
        for answers in ("", "q\n", "1\n"):
            with self.subTest(answers=answers):
                before, state_before = self.tree_contents(self.wc), self.tree_contents(self.state)
                patches_before = self.tree_contents(self.patches)
                result = self.cli("resolve", "story.txt", "--accept", "mine-conflict",
                                  input=answers)
                self.assertIn("shared local", result.stdout)
                self.assertIn("Apply resolution", result.stdout)
                self.assertEqual(self.tree_contents(self.wc), before)
                self.assertEqual(self.tree_contents(self.state), state_before)
                self.assertEqual(self.tree_contents(self.patches), patches_before)

    def test_invalid_nonconflicted_and_ignored_paths_cannot_mutate_working_copy(self):
        self.make_text_conflict()
        for name in ("../story.txt", ".svn/wc.db", "removed.txt", str(self.root / "outside.txt")):
            with self.subTest(name=name):
                before, state_before = self.tree_contents(self.wc), self.tree_contents(self.state)
                self.cli("resolve", name, "--accept", "mine-full", success=False)
                self.assertEqual(self.tree_contents(self.wc), before)
                self.assertEqual(self.tree_contents(self.state), state_before)
        self.svn("changelist", "ignore-on-commit", "story.txt")
        before, state_before = self.tree_contents(self.wc), self.tree_contents(self.state)
        self.cli("resolve", "story.txt", "--accept", "mine-full", success=False)
        menu = self.cli("resolve", input="").stdout
        self.assertNotIn("1.", menu)
        self.assertEqual(self.tree_contents(self.wc), before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_absolute_path_and_at_filename_are_resolved_as_literal_targets(self):
        name = "upgrade@141.sql"
        self.make_text_conflict(name)
        self.cli("resolve", str(self.wc / name), "--accept", "theirs-conflict", input="2\n")
        self.assertNotEqual(self.status_for(name).get("item"), "conflicted")
        self.assertIn("local change", self.read(name))
        self.assertIn("shared incoming", self.read(name))

    def test_tree_conflict_requires_manual_working_acceptance(self):
        self.new_branch()
        self.write("story.txt", "local edit before incoming deletion\n")
        upstream = self.upstream()
        self.run_process(["svn", "delete", "story.txt"], cwd=upstream)
        self.run_process(["svn", "commit", "-m", "incoming deletion"], cwd=upstream)
        self.svn("update", "--accept", "postpone")
        self.assertEqual(self.status_for("story.txt").get("tree-conflicted"), "true")
        before, state_before = self.tree_contents(self.wc), self.tree_contents(self.state)
        self.cli("resolve", "story.txt", "--accept", "mine-full", success=False)
        self.assertEqual(self.tree_contents(self.wc), before)
        self.assertEqual(self.tree_contents(self.state), state_before)

        self.cli("resolve", "story.txt", "--accept", "working", input="2\n")

        self.assertNotEqual(self.status_for("story.txt").get("tree-conflicted"), "true")
        self.assertEqual(self.read("story.txt"), "local edit before incoming deletion\n")
        self.assertTrue(self.tree_contents(self.state / "resolutions"))

    def test_accept_without_path_is_rejected_without_resolution(self):
        self.make_text_conflict()
        before, state_before = self.tree_contents(self.wc), self.tree_contents(self.state)
        self.cli("resolve", "--accept", "mine-full", input="", success=False)
        self.assertEqual(self.tree_contents(self.wc), before)
        self.assertEqual(self.tree_contents(self.state), state_before)


if __name__ == "__main__":
    unittest.main()
