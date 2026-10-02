"""Integration coverage using isolated, local SVN repositories."""

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN = PROJECT_ROOT / "main.py"


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"),
                     "svn and svnadmin must be on PATH")
class GitSvnIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gitsvn-tests-", dir=PROJECT_ROOT)
        self.root = Path(self.temp.name).resolve()
        # TemporaryDirectory recursively removes its contents at cleanup.
        # Resolve and verify its location before authorizing that cleanup.
        self.assertTrue(self.root.is_relative_to(PROJECT_ROOT.resolve()))
        self.assertNotEqual(self.root, PROJECT_ROOT.resolve())
        self.addCleanup(self.temp.cleanup)
        self.repo = self.root / "repo"
        self.wc = self.root / "wc"
        self.patches = self.root / "patches"
        self.state = self.root / "state"
        self.run_process(["svnadmin", "create", str(self.repo)])
        seed = self.root / "seed"
        seed.mkdir()
        for name, content in {
            "story.txt": "original\n",
            "removed.txt": "remove me\n",
            "ignored.txt": "ignored original\n",
            "properties.txt": "property target\n",
        }.items():
            (seed / name).write_text(content, encoding="utf-8")
        self.repo_url = self.repo.as_uri()
        self.run_process(["svn", "import", str(seed), self.repo_url, "-m", "initial"])
        self.run_process(["svn", "checkout", self.repo_url, str(self.wc)])

    def run_process(self, args, cwd=None, input=None, success=True):
        result = subprocess.run(args, cwd=cwd or self.root, input=input,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=45)
        if success:
            self.assertEqual(result.returncode, 0,
                             f"Command failed: {args!r}\n{result.stdout}\n{result.stderr}")
        else:
            self.assertNotEqual(result.returncode, 0,
                                f"Command unexpectedly succeeded: {args!r}\n{result.stdout}")
        return result

    def cli(self, *args, input=None, success=True):
        return self.run_process([
            sys.executable, str(MAIN), "--svn-root", str(self.wc),
            "--patch-root", str(self.patches), "--state-dir", str(self.state),
            *args,
        ], input=input, success=success)

    def svn(self, *args, success=True):
        return self.run_process(["svn", *args], cwd=self.wc, success=success)

    def write(self, name, text):
        path = self.wc / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def read(self, name):
        return (self.wc / name).read_text(encoding="utf-8")

    def snapshots(self, branch):
        return sorted((self.patches / branch).glob("*.patch"))

    def new_branch(self, name="18814"):
        self.cli("branch", name)
        self.cli("switch", name)

    def commit(self, message):
        self.cli("commit", "-m", message)
        return self.snapshots("18814")[-1].stem

    def svn_status(self):
        document = ET.fromstring(self.svn("status", "--xml").stdout)
        return {entry.get("path").replace("\\", "/"):
                entry.find("wc-status").get("item")
                for entry in document.findall(".//entry")}

    def tree_contents(self, root):
        if not root.exists():
            return {}
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def test_branch_initializes_clean_trunk_without_switching(self):
        self.cli("status")
        self.write("story.txt", "dirty trunk\n")
        self.cli("branch", "18814")
        self.assertEqual(self.read("story.txt"), "dirty trunk\n")
        self.assertIn("trunk", self.cli("status").stdout)
        initial = self.snapshots("18814")
        self.assertEqual(len(initial), 1)
        self.assertRegex(initial[0].stem, r"^\d{8}_\d{6}_\d{6}$")
        self.assertEqual(initial[0].read_text(encoding="utf-8").strip(), "")
        self.assertTrue(initial[0].with_suffix(".txt").is_file())
        self.assertTrue(initial[0].with_suffix(".json").is_file())
        json.loads(initial[0].with_suffix(".json").read_text(encoding="utf-8"))
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "original\n")
        self.cli("switch", "trunk")
        self.assertEqual(self.read("story.txt"), "dirty trunk\n")

    def test_branch_roundtrip_and_historical_revert_preserve_selected_head(self):
        self.new_branch()
        self.write("story.txt", "first version\n")
        first = self.commit("first change")
        self.write("story.txt", "second version\n")
        second = self.commit("second change")
        self.assertNotEqual(first, second)
        self.cli("switch", "trunk")
        self.assertEqual(self.read("story.txt"), "original\n")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "second version\n")
        self.cli("revert", "18814", first)
        self.assertEqual(self.read("story.txt"), "first version\n")
        self.cli("switch", "trunk")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "first version\n")
        log = self.cli("log", "18814").stdout
        self.assertIn(first, log)
        self.assertIn(second, log)
        self.assertIn("first change", log)
        self.assertIn("second change", log)

    def test_switch_autosaves_uncommitted_outgoing_changes(self):
        self.cli("branch", "18814")
        self.write("story.txt", "unsaved trunk work\n")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "original\n")
        self.assertTrue(any("unsaved trunk work" in path.read_text(encoding="utf-8")
                            for path in self.snapshots("trunk")))
        self.write("story.txt", "unsaved branch work\n")
        self.cli("switch", "trunk")
        self.assertEqual(self.read("story.txt"), "unsaved trunk work\n")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "unsaved branch work\n")

    def test_ignored_edits_and_untracked_file_inside_added_directory_survive(self):
        self.new_branch()
        self.svn("changelist", "ignore-on-commit", "ignored.txt")
        self.write("ignored.txt", "private ignored edit\n")
        self.write("newdir/added.txt", "captured addition\n")
        self.cli("add", "newdir")
        self.write("newdir/scratch.txt", "untracked scratch\n")
        snapshot = self.commit("added directory")
        patch_text = (self.patches / "18814" / f"{snapshot}.patch").read_text(encoding="utf-8")
        self.assertNotIn("private ignored edit", patch_text)
        self.assertNotIn("scratch.txt", patch_text)
        self.cli("switch", "trunk")
        self.assertEqual(self.read("ignored.txt"), "private ignored edit\n")
        self.assertEqual(self.read("newdir/scratch.txt"), "untracked scratch\n")
        self.assertFalse((self.wc / "newdir/added.txt").exists())
        self.cli("switch", "18814")
        self.assertEqual(self.read("ignored.txt"), "private ignored edit\n")
        self.assertEqual(self.read("newdir/scratch.txt"), "untracked scratch\n")
        self.assertEqual(self.read("newdir/added.txt"), "captured addition\n")
        self.assertEqual(self.svn_status().get("newdir/added.txt"), "added")

    def test_property_only_snapshot_roundtrip(self):
        self.new_branch()
        self.svn("propset", "custom:flag", "branch value", "properties.txt")
        self.commit("property change")
        self.cli("switch", "trunk")
        self.svn("propget", "custom:flag", "properties.txt", success=False)
        self.cli("switch", "18814")
        self.assertEqual(self.svn("propget", "custom:flag", "properties.txt").stdout.strip(),
                         "branch value")
        self.assertEqual(self.read("properties.txt"), "property target\n")

    def test_text_addition_deletion_and_empty_file_and_directory_roundtrip(self):
        self.new_branch()
        self.write("new.txt", "new text\n")
        (self.wc / "empty.txt").touch()
        (self.wc / "emptydir").mkdir()
        self.cli("add", "new.txt", "empty.txt", "emptydir")
        self.svn("delete", "removed.txt")
        self.commit("files and empty directory")
        self.cli("switch", "trunk")
        self.assertEqual(self.read("removed.txt"), "remove me\n")
        for name in ("new.txt", "empty.txt", "emptydir"):
            self.assertFalse((self.wc / name).exists(), name)
        self.cli("switch", "18814")
        self.assertEqual(self.read("new.txt"), "new text\n")
        self.assertEqual((self.wc / "empty.txt").read_bytes(), b"")
        self.assertTrue((self.wc / "emptydir").is_dir())
        self.assertFalse((self.wc / "removed.txt").exists())
        status = self.svn_status()
        for name in ("new.txt", "empty.txt", "emptydir"):
            self.assertEqual(status.get(name), "added", status)
        self.assertEqual(status.get("removed.txt"), "deleted", status)

    def test_patch_conflict_after_pull_rolls_back_outgoing_work(self):
        self.new_branch()
        self.write("story.txt", "saved branch change\n")
        self.commit("branch story")
        self.cli("switch", "trunk")
        upstream = self.root / "upstream"
        self.run_process(["svn", "checkout", self.repo_url, str(upstream)])
        (upstream / "story.txt").write_text("upstream change\n", encoding="utf-8")
        self.run_process(["svn", "commit", "-m", "upstream edit"], cwd=upstream)
        self.cli("pull")
        self.assertEqual(self.read("story.txt"), "upstream change\n")
        self.write("removed.txt", "outgoing local change\n")
        self.cli("switch", "18814", success=False)
        self.assertEqual(self.read("story.txt"), "upstream change\n")
        self.assertEqual(self.read("removed.txt"), "outgoing local change\n")
        self.assertIn("trunk", self.cli("status").stdout)
        self.assertNotIn("conflicted", self.svn_status().values())
        self.assertFalse(list(self.wc.rglob("*.svnpatch.rej")))

    def test_interactive_cancel_lists_messages_without_mutating_anything(self):
        self.new_branch()
        self.write("story.txt", "saved version\n")
        snapshot = self.commit("visible snapshot message")
        self.write("story.txt", "still unsaved\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        result = self.cli("revert", "18814", input="q\n")
        self.assertIn(snapshot, result.stdout)
        self.assertIn("visible snapshot message", result.stdout)
        self.assertEqual(self.read("story.txt"), "still unsaved\n")
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_invalid_branch_and_snapshot_paths_are_rejected_before_mutation(self):
        self.new_branch()
        self.write("story.txt", "keep this work\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        for name in ("../escape", "..", "nested/ticket", str(self.root / "absolute")):
            with self.subTest(name=name):
                self.cli("branch", name, success=False)
        self.cli("revert", "18814", "../escape", success=False)
        self.assertEqual(self.read("story.txt"), "keep this work\n")
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)
        self.assertFalse((self.root / "escape").exists())
        self.assertFalse((self.root / "absolute").exists())

    def test_legacy_patch_without_message_is_listed_and_restored(self):
        self.cli("status")
        self.write("story.txt", "legacy edit\n")
        diff = self.svn("diff").stdout
        self.svn("revert", "story.txt")
        branch = self.patches / "legacy"
        branch.mkdir(parents=True)
        (branch / "20200101_120000_000000.patch").write_text(diff, encoding="utf-8")
        self.assertIn("20200101_120000_000000", self.cli("log", "legacy").stdout)
        self.cli("switch", "legacy")
        self.assertEqual(self.read("story.txt"), "legacy edit\n")

    def test_patch_path_traversal_is_rejected_before_revert(self):
        self.cli("status")
        self.write("story.txt", "must survive\n")
        branch = self.patches / "malicious"
        branch.mkdir(parents=True)
        snapshot = branch / "20200101_120000_000000.patch"
        snapshot.write_text(
            "Index: ../outside.txt\n"
            "===================================================================\n"
            "--- ../outside.txt\t(nonexistent)\n"
            "+++ ../outside.txt\t(working copy)\n"
            "@@ -0,0 +1 @@\n"
            "+escaped\n", encoding="utf-8")
        self.cli("switch", "malicious", success=False)
        self.assertEqual(self.read("story.txt"), "must survive\n")
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertIn("trunk", self.cli("status").stdout)

    def test_binary_addition_is_rejected_without_losing_changes(self):
        self.cli("branch", "18814")
        self.write("story.txt", "preserve text\n")
        data = b"\x00\x01\x02binary\xff"
        (self.wc / "binary.dat").write_bytes(data)
        self.cli("add", "binary.dat")
        self.cli("switch", "18814", success=False)
        self.assertEqual((self.wc / "binary.dat").read_bytes(), data)
        self.assertEqual(self.read("story.txt"), "preserve text\n")
        self.assertEqual(self.svn_status().get("binary.dat"), "added")
        self.assertIn("trunk", self.cli("status").stdout)

    def test_svn_copy_is_rejected_without_losing_changes(self):
        self.cli("branch", "18814")
        self.write("story.txt", "preserve source\n")
        self.svn("copy", "story.txt", "copied.txt")
        self.cli("switch", "18814", success=False)
        self.assertEqual(self.read("story.txt"), "preserve source\n")
        self.assertEqual(self.read("copied.txt"), "preserve source\n")
        self.assertEqual(self.svn_status().get("copied.txt"), "added")

    def test_sql_comment_and_binary_marker_source_strings_roundtrip(self):
        self.write("story.txt", "-- user: value\nCannot display: ordinary source\n")
        self.svn("commit", "-m", "source fixture")
        self.new_branch()
        expected = "++ added source\nCannot display: modified source\n"
        self.write("story.txt", expected)
        snapshot = self.commit("SQL comment and source strings")
        patch = (self.patches / "18814" / f"{snapshot}.patch").read_text(encoding="utf-8")
        self.assertIn("--- user: value", patch)
        self.assertIn("+Cannot display: modified source", patch)
        self.cli("switch", "trunk")
        self.assertEqual(self.read("story.txt"),
                         "-- user: value\nCannot display: ordinary source\n")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), expected)

    def test_property_payload_resembling_patch_header_roundtrip(self):
        self.new_branch()
        self.svn("propset", "custom:note", "++ value: thing", "properties.txt")
        snapshot = self.commit("header-like property payload")
        patch = (self.patches / "18814" / f"{snapshot}.patch").read_text(encoding="utf-8")
        self.assertIn("+++ value: thing", patch)
        self.cli("switch", "trunk")
        self.svn("propget", "custom:note", "properties.txt", success=False)
        self.cli("switch", "18814")
        self.assertEqual(self.svn("propget", "custom:note", "properties.txt").stdout.strip(),
                         "++ value: thing")

    def test_truncated_file_with_property_restores_as_modified_empty_file(self):
        self.new_branch()
        self.write("story.txt", "")
        self.svn("propset", "custom:flag", "empty but present", "story.txt")
        self.commit("truncate and set property")
        self.cli("switch", "trunk")
        self.assertEqual(self.read("story.txt"), "original\n")
        self.svn("propget", "custom:flag", "story.txt", success=False)
        self.cli("switch", "18814")
        self.assertTrue((self.wc / "story.txt").is_file())
        self.assertEqual((self.wc / "story.txt").read_bytes(), b"")
        self.assertEqual(self.svn_status().get("story.txt"), "modified")
        self.assertEqual(self.svn("propget", "custom:flag", "story.txt").stdout.strip(),
                         "empty but present")

    def test_deleted_tracked_directory_and_empty_file_roundtrip(self):
        self.write("tracked/nested/file.txt", "inside directory\n")
        self.write("emptybase.txt", "")
        self.svn("add", "tracked", "emptybase.txt")
        self.svn("commit", "-m", "directory and empty-file fixture")
        self.new_branch()
        self.svn("delete", "tracked", "emptybase.txt")
        self.commit("delete tracked directory and empty file")
        self.cli("switch", "trunk")
        self.assertEqual(self.read("tracked/nested/file.txt"), "inside directory\n")
        self.assertEqual((self.wc / "emptybase.txt").read_bytes(), b"")
        self.cli("switch", "18814")
        self.assertFalse((self.wc / "tracked").exists())
        self.assertFalse((self.wc / "emptybase.txt").exists())
        status = self.svn_status()
        self.assertEqual(status.get("tracked"), "deleted", status)
        self.assertEqual(status.get("emptybase.txt"), "deleted", status)

    def test_manual_revert_to_base_updates_branch_head_when_switching(self):
        self.new_branch()
        self.write("story.txt", "previously saved work\n")
        self.commit("dirty snapshot")
        self.svn("revert", "story.txt")
        self.cli("switch", "trunk")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "original\n")
        self.assertEqual(self.svn_status(), {})

    def test_explicit_clean_commit_saves_empty_snapshot(self):
        self.new_branch()
        self.write("story.txt", "old saved work\n")
        self.commit("old work")
        self.svn("revert", "story.txt")
        snapshot = self.commit("clean branch")
        patch = self.patches / "18814" / f"{snapshot}.patch"
        self.assertEqual(patch.read_bytes(), b"")
        metadata = json.loads(patch.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["entries"], [])
        self.assertIn("clean branch", self.cli("log").stdout)
        self.cli("switch", "trunk")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "original\n")
        self.assertEqual(self.svn_status(), {})

    def test_invalid_empty_properties_metadata_rejected_before_revert(self):
        self.new_branch()
        self.write("story.txt", "saved branch work\n")
        snapshot = self.commit("will corrupt metadata")
        self.cli("switch", "trunk")
        metadata_path = self.patches / "18814" / f"{snapshot}.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["empty_properties"] = []
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        self.write("story.txt", "preserve current edits\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        self.cli("revert", "18814", snapshot, success=False)
        self.assertEqual(self.read("story.txt"), "preserve current edits\n")
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_embedded_property_patch_cannot_affect_another_file(self):
        self.new_branch()
        self.write("story.txt", "")
        self.svn("propset", "custom:flag", "flag", "story.txt")
        snapshot = self.commit("empty file with properties")
        self.cli("switch", "trunk")
        metadata_path = self.patches / "18814" / f"{snapshot}.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        escaped_patch = (
            "Index: removed.txt\n"
            "===================================================================\n"
            "--- removed.txt\t(revision 1)\n"
            "+++ removed.txt\t(working copy)\n"
            "@@ -1 +1 @@\n-remove me\n+wrong file\n"
        )
        metadata["empty_properties"]["story.txt"] = base64.b64encode(
            escaped_patch.encode("utf-8")).decode("ascii")
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        self.write("story.txt", "preserve outgoing file\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        self.cli("revert", "18814", snapshot, success=False)
        self.assertEqual(self.read("story.txt"), "preserve outgoing file\n")
        self.assertEqual(self.read("removed.txt"), "remove me\n")
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_uppercase_svn_administrative_path_rejected_before_revert(self):
        self.cli("status")
        branch = self.patches / "malicious"
        branch.mkdir(parents=True)
        (branch / "20200101_120000_000000.patch").write_text(
            "Index: .SVN/gitsvn-test-marker\n"
            "===================================================================\n"
            "--- .SVN/gitsvn-test-marker\t(nonexistent)\n"
            "+++ .SVN/gitsvn-test-marker\t(working copy)\n"
            "@@ -0,0 +1 @@\n+unsafe\n", encoding="utf-8")
        self.write("story.txt", "preserve outgoing work\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        self.cli("switch", "malicious", success=False)
        self.assertEqual(self.read("story.txt"), "preserve outgoing work\n")
        self.assertFalse((self.wc / ".svn/gitsvn-test-marker").exists())
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_existing_svn_ignored_file_cannot_be_overwritten_by_snapshot(self):
        self.new_branch()
        self.write("blocked.txt", "")
        self.cli("add", "blocked.txt")
        self.commit("empty file addition")
        self.cli("switch", "trunk")
        self.svn("propset", "svn:ignore", "blocked.txt", ".")
        self.write("blocked.txt", "preserve ignored file contents\n")
        self.write("story.txt", "preserve tracked edits\n")
        document = ET.fromstring(self.svn("status", "--xml", "--no-ignore").stdout)
        ignored = {entry.get("path"): entry.find("wc-status").get("item")
                   for entry in document.findall(".//entry")}
        self.assertEqual(ignored.get("blocked.txt"), "ignored")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        self.cli("switch", "18814", success=False)
        self.assertEqual(self.read("blocked.txt"), "preserve ignored file contents\n")
        self.assertEqual(self.read("story.txt"), "preserve tracked edits\n")
        self.assertEqual(self.svn("propget", "svn:ignore", ".").stdout.strip(), "blocked.txt")
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_tree_conflict_blocks_switch_without_losing_local_edit(self):
        self.cli("branch", "18814")
        self.write("story.txt", "local edit before upstream deletion\n")
        upstream = self.root / "upstream"
        self.run_process(["svn", "checkout", self.repo_url, str(upstream)])
        self.run_process(["svn", "delete", "story.txt"], cwd=upstream)
        self.run_process(["svn", "commit", "-m", "upstream deletion"], cwd=upstream)
        result = self.cli("pull", success=False)
        self.assertIn("conflict", (result.stdout + result.stderr).lower())
        document = ET.fromstring(self.svn("status", "--xml").stdout)
        story = next(entry for entry in document.findall(".//entry")
                     if entry.get("path") == "story.txt")
        self.assertEqual(story.find("wc-status").get("tree-conflicted"), "true")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        self.cli("switch", "18814", success=False)
        self.assertEqual(self.read("story.txt"), "local edit before upstream deletion\n")
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_mixed_text_and_property_patch_conflict_rolls_back_outgoing_work(self):
        self.svn("propset", "custom:flag", "base property", "properties.txt")
        self.svn("commit", "-m", "base property fixture")
        self.new_branch()
        self.write("properties.txt", "branch text\n")
        self.svn("propset", "custom:flag", "branch property", "properties.txt")
        self.commit("text and property snapshot")
        self.cli("switch", "trunk")
        upstream = self.root / "upstream"
        self.run_process(["svn", "checkout", self.repo_url, str(upstream)])
        self.run_process(["svn", "propset", "custom:flag", "upstream property",
                          "properties.txt"], cwd=upstream)
        self.run_process(["svn", "commit", "-m", "upstream property edit"], cwd=upstream)
        self.cli("pull")
        self.write("removed.txt", "outgoing work must survive\n")
        result = self.cli("switch", "18814", success=False)
        self.assertIn("conflict", (result.stdout + result.stderr).lower())
        self.assertEqual(self.read("properties.txt"), "property target\n")
        self.assertEqual(self.svn("propget", "custom:flag", "properties.txt").stdout.strip(),
                         "upstream property")
        self.assertEqual(self.read("removed.txt"), "outgoing work must survive\n")
        document = ET.fromstring(self.svn("status", "--xml").stdout)
        self.assertFalse(any(entry.find("wc-status").get("props") == "conflicted"
                             for entry in document.findall(".//entry")))
        self.assertIn("trunk", self.cli("status").stdout)

    def test_scheduled_file_deletion_with_kept_local_data_blocks_switch(self):
        self.cli("branch", "18814")
        self.write("removed.txt", "kept local data\n")
        self.svn("delete", "--force", "--keep-local", "removed.txt")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        self.cli("switch", "18814", success=False)
        self.assertEqual(self.read("removed.txt"), "kept local data\n")
        self.assertEqual(self.svn_status().get("removed.txt"), "deleted")
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_scheduled_directory_deletion_with_hidden_local_data_blocks_switch(self):
        self.write("tracked/file.txt", "tracked directory content\n")
        self.svn("add", "tracked")
        self.svn("commit", "-m", "kept-local directory fixture")
        self.cli("branch", "18814")
        self.svn("delete", "--keep-local", "tracked")
        self.write("tracked/scratch.txt", "hidden untracked data\n")
        self.write("story.txt", "unrelated local edits\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        self.cli("switch", "18814", success=False)
        self.assertEqual(self.read("tracked/file.txt"), "tracked directory content\n")
        self.assertEqual(self.read("tracked/scratch.txt"), "hidden untracked data\n")
        self.assertEqual(self.read("story.txt"), "unrelated local edits\n")
        self.assertEqual(self.svn_status().get("tracked"), "deleted")
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_patch_headers_stay_relative_to_svn_root_from_external_directory(self):
        relative = "masa/applications/www.test/UnitTests/SViewPriorityTest.cs"
        (self.wc / relative).parent.mkdir(parents=True)
        self.svn("add", "masa")
        self.svn("commit", "-m", "nested directory fixture")
        self.new_branch()
        self.write(relative, "namespace UnitTests { class SViewPriorityTest {} }\n")
        # cli() runs from self.root, outside the SVN working copy.
        self.assertFalse(self.root.is_relative_to(self.wc))
        self.cli("add", relative)
        snapshot = self.commit("nested file from external directory")
        patch = (self.patches / "18814" / f"{snapshot}.patch").read_text(encoding="utf-8")
        lines = patch.splitlines()
        self.assertEqual([line for line in lines if line.startswith("Index: ")],
                         [f"Index: {relative}"])
        for prefix in ("--- ", "+++ "):
            self.assertEqual([line.split("\t", 1)[0] for line in lines
                              if line.startswith(prefix)], [prefix + relative])
        self.assertNotIn(str(self.wc), patch)
        self.assertNotIn(str(self.wc).replace("\\", "/"), patch)


if __name__ == "__main__":
    unittest.main()
