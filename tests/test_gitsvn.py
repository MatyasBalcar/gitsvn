"""Integration coverage using isolated, local SVN repositories."""

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from gitsvn.app import GitSvn
from gitsvn.cli import main
from gitsvn.models import GitSvnError
from gitsvn.setup import register_path
from gitsvn.terminal import pick_branch


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

    def configured_cli(self, config_path, *args, cwd=None, success=True):
        return self.run_process([
            sys.executable, str(MAIN), "--config", str(config_path), *args,
        ], cwd=cwd, success=success)

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

    def configure(self, **settings):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "config.json").write_text(json.dumps(settings), encoding="utf-8")

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

    def unsaved_paths(self):
        app = GitSvn(self.wc, self.patches, self.state)
        return app.unsaved_changes(app.changes(app.entries()))

    def tree_contents(self, root):
        if not root.exists():
            return {}
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def init_program(self):
        program = self.root / "program with spaces"
        program.mkdir(exist_ok=True)
        for name in ("main.py", "gitsvn.cmd", "install.ps1"):
            shutil.copy2(PROJECT_ROOT / name, program / name)
        shutil.copytree(PROJECT_ROOT / "gitsvn", program / "gitsvn", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        return program

    def init_cli(self, answers, *args, cwd=None, default_root=None, installer_error=None):
        program = self.init_program()
        output, errors = io.StringIO(), io.StringIO()
        previous_dir = Path.cwd()
        try:
            os.chdir(cwd or self.root)
            with patch("gitsvn.config.program_directory", return_value=program), \
                    patch("gitsvn.config.DEFAULT_SVN_ROOT", default_root or self.wc), \
                    patch("gitsvn.config.DEFAULT_PATCH_ROOT", self.patches), \
                    patch("gitsvn.cli.sys.stdin", io.StringIO(answers)), \
                    patch("gitsvn.cli.sys.stdout", output), \
                    patch("gitsvn.cli.sys.stderr", errors), \
                    patch("gitsvn.setup.register_path") as installer:
                installer.side_effect = installer_error
                code = main([*map(str, args), "init"])
        finally:
            os.chdir(previous_dir)
        return code, output.getvalue(), errors.getvalue(), installer

    def test_init_detects_checkout_and_creates_default_config_without_snapshots(self):
        nested = self.wc / "nested"
        nested.mkdir()
        status_before = ET.canonicalize(self.svn("status", "--xml").stdout)

        code, output, errors, installer = self.init_cli(
            "\n" * 4, cwd=nested, default_root=self.root / "unused-fallback")

        self.assertEqual(code, 0, errors)
        config_path = self.root / "program with spaces" / ".gitsvn" / "config.json"
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), {
            "svn_root": str(self.wc), "patch_root": str(self.patches),
            "state_dir": str(config_path.parent), "autosave": True,
        })
        self.assertTrue(self.patches.is_dir())
        self.assertEqual(self.tree_contents(self.patches), {})
        self.assertFalse((config_path.parent / "state.json").exists())
        installer.assert_called_once_with()
        self.assertIn("Open a new terminal", output)
        self.assertEqual(ET.canonicalize(self.svn("status", "--xml").stdout), status_before)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        status = self.run_process([sys.executable, str(config_path.parent.parent / "main.py"),
                                   "status"], cwd=elsewhere)
        self.assertIn("On branch trunk", status.stdout)

    def test_init_uses_builtin_root_when_outside_a_checkout(self):
        code, output, errors, _ = self.init_cli("\n" * 4)
        self.assertEqual(code, 0, errors)
        self.assertIn(f"SVN working-copy root [{self.wc}]", output)
        config_path = self.root / "program with spaces" / ".gitsvn" / "config.json"
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["svn_root"], str(self.wc))

    def test_init_preserves_existing_settings_state_and_branch_history(self):
        self.configure(svn_root=str(self.wc), patch_root=str(self.patches), state_dir=".",
                       autosave=False, custom_setting="preserve me")
        self.new_branch()
        self.write("story.txt", "saved branch work\n")
        self.commit("saved work")
        self.write("story.txt", "unsaved work stays in place\n")
        state_before = (self.state / "state.json").read_bytes()
        patches_before = self.tree_contents(self.patches)
        config_path = self.state / "config.json"
        for _ in range(2):
            code, output, errors, installer = self.init_cli("\n" * 4, "--config", config_path)
            self.assertEqual(code, 0, errors)
            installer.assert_called_once_with()
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(config["autosave"])
            self.assertEqual(config["custom_setting"], "preserve me")
            self.assertEqual(config["state_dir"], str(self.state))
            self.assertEqual((self.state / "state.json").read_bytes(), state_before)
            self.assertEqual(self.tree_contents(self.patches), patches_before)
            self.assertEqual(self.read("story.txt"), "unsaved work stays in place\n")
            self.assertIn(f'gitsvn --config "{config_path}"', output)

    def test_init_cli_overrides_existing_config_defaults(self):
        config_path = self.root / "settings" / "project.json"
        config_path.parent.mkdir()
        config_path.write_text(json.dumps({
            "svn_root": "old-wc", "patch_root": "old-patches", "state_dir": "old-state",
            "autosave": False,
        }), encoding="utf-8")
        new_state = self.root / "new-state"
        new_state.mkdir()
        # An explicit --config must not read this unrelated configuration.
        (new_state / "config.json").write_text("{", encoding="utf-8")

        code, _, errors, _ = self.init_cli(
            "\n" * 4, "--config", config_path, "--svn-root", "wc",
            "--patch-root", "new-patches", "--state-dir", "new-state")

        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), {
            "svn_root": str(self.wc), "patch_root": str(self.root / "new-patches"),
            "state_dir": str(new_state), "autosave": False,
        })
        self.assertEqual((new_state / "config.json").read_text(encoding="utf-8"), "{")
        self.configured_cli(config_path, "status")

    def test_init_accepts_quoted_relative_paths_and_autosave_choice(self):
        code, _, errors, _ = self.init_cli("'wc'\n\"patch folder\"\n'state folder'\nno\n")
        self.assertEqual(code, 0, errors)
        config_path = self.root / "program with spaces" / ".gitsvn" / "config.json"
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), {
            "svn_root": str(self.wc), "patch_root": str(self.root / "patch folder"),
            "state_dir": str(self.root / "state folder"), "autosave": False,
        })
        self.assertTrue((self.root / "patch folder").is_dir())
        self.assertTrue((self.root / "state folder").is_dir())

    def test_init_invalid_paths_and_autosave_are_reprompted(self):
        not_folder = self.root / "not-a-folder"
        not_folder.write_text("keep me", encoding="utf-8")
        forbidden_patch, forbidden_state = self.wc / "patches", self.wc / "state"
        patch_root, state_dir = self.root / "valid-patches", self.root / "valid-state"
        answers = "\n".join(map(str, [self.root, self.wc, forbidden_patch, not_folder,
                                     patch_root, forbidden_state, state_dir, "maybe", "false"])) + "\n"

        code, output, errors, installer = self.init_cli(answers)

        self.assertEqual(code, 0, errors)
        self.assertIn("Invalid svn working-copy root", output)
        self.assertIn("outside the SVN working copy", output)
        self.assertIn("Not a folder", output)
        self.assertIn("Enter true or false", output)
        self.assertFalse(forbidden_patch.exists())
        self.assertFalse(forbidden_state.exists())
        self.assertEqual(not_folder.read_text(encoding="utf-8"), "keep me")
        self.assertTrue(patch_root.is_dir())
        self.assertTrue(state_dir.is_dir())
        installer.assert_called_once_with()

    def test_init_requires_separate_state_for_changed_workspace_paths(self):
        self.configure(svn_root=str(self.wc), patch_root=str(self.patches), state_dir=".",
                       autosave=False)
        self.new_branch()
        config_path = self.state / "config.json"
        state_before = (self.state / "state.json").read_bytes()
        config_before = config_path.read_bytes()
        patches_before = self.tree_contents(self.patches)
        new_patches, new_state = self.root / "new-patches", self.root / "new-state"
        args = ("--config", config_path, "--patch-root", new_patches)

        code, output, _, installer = self.init_cli("\n" * 3, *args)
        self.assertEqual(code, 0)
        self.assertIn("separate --state-dir", output)
        self.assertIn("Initialization cancelled", output)
        installer.assert_not_called()
        self.assertEqual(config_path.read_bytes(), config_before)
        self.assertFalse(new_patches.exists())

        code, output, errors, installer = self.init_cli(f"\n\n\n{new_state}\n\n", *args)
        self.assertEqual(code, 0, errors)
        self.assertIn("separate --state-dir", output)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["state_dir"], str(new_state))
        self.assertEqual(config["patch_root"], str(new_patches))
        self.assertEqual((self.state / "state.json").read_bytes(), state_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertFalse((new_state / "state.json").exists())
        installer.assert_called_once_with()

    def test_init_eof_and_keyboard_interrupt_do_not_save_or_register_path(self):
        for answers in ("", "\n", "\n\n", "\n\n\n"):
            with self.subTest(answers=answers):
                code, output, errors, installer = self.init_cli(answers)
                self.assertEqual(code, 0, errors)
                self.assertIn("Initialization cancelled", output)
                installer.assert_not_called()
                self.assertFalse((self.root / "program with spaces" / ".gitsvn").exists())
                self.assertFalse(self.patches.exists())
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            code, _, errors, installer = self.init_cli("")
        self.assertEqual(code, 130)
        self.assertIn("Interrupted", errors)
        installer.assert_not_called()
        self.assertFalse((self.root / "program with spaces" / ".gitsvn").exists())
        self.assertFalse(self.patches.exists())

    def test_init_path_registration_failure_retains_saved_configuration(self):
        code, output, errors, installer = self.init_cli(
            "\n" * 4, installer_error=GitSvnError("test installer failure"))
        self.assertEqual(code, 1)
        config_path = self.root / "program with spaces" / ".gitsvn" / "config.json"
        self.assertTrue(config_path.is_file())
        self.assertTrue(self.patches.is_dir())
        self.assertIn("Saved configuration", output)
        self.assertIn("Configuration saved, but PATH registration failed", errors)
        self.assertIn("Retry with: powershell", errors)
        installer.assert_called_once_with()
        self.configured_cli(config_path, "status")

    def test_init_failed_config_write_preserves_original_config(self):
        self.configure(svn_root=str(self.wc), patch_root=str(self.patches), state_dir=".",
                       autosave=False)
        config_path = self.state / "config.json"
        state_before = self.tree_contents(self.state)
        with patch("gitsvn.config.os.replace", side_effect=OSError("test write failure")):
            code, _, errors, installer = self.init_cli("\n" * 4, "--config", config_path)
        self.assertEqual(code, 1)
        self.assertIn("test write failure", errors)
        self.assertEqual(self.tree_contents(self.state), state_before)
        installer.assert_not_called()

    def test_init_cannot_overwrite_branch_state_as_configuration(self):
        self.new_branch()
        state_before = self.tree_contents(self.state)
        patches_before = self.tree_contents(self.patches)
        code, _, errors, installer = self.init_cli("", "--config", self.state / "state.json")
        self.assertEqual(code, 1)
        self.assertIn("reserved for branch state", errors)
        self.assertEqual(self.tree_contents(self.state), state_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        installer.assert_not_called()

    def test_init_installer_uses_bundled_script_and_reports_failure(self):
        program = self.init_program()
        output = io.StringIO()
        with patch("gitsvn.config.program_directory", return_value=program), \
                patch("gitsvn.setup.subprocess.run") as run, \
                patch("gitsvn.cli.sys.stdout", output):
            run.return_value = subprocess.CompletedProcess([], 0, b"registered\n", b"")
            register_path()
            self.assertEqual(run.call_args.args[0], [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(program / "install.ps1"),
            ])
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertIn("registered", output.getvalue())
            run.return_value = subprocess.CompletedProcess([], 1, b"", b"test installer error")
            with self.assertRaisesRegex(GitSvnError, "test installer error"):
                register_path()

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

    def test_branch_cannot_use_installed_package_folder_as_snapshot_storage(self):
        program = self.init_program()
        program_before = self.tree_contents(program)
        self.write("story.txt", "pending work must survive reserved branch\n")

        result = self.run_process([
            sys.executable, str(program / "main.py"), "--svn-root", str(self.wc),
            "--patch-root", str(program), "--state-dir", str(self.state),
            "branch", "gitsvn",
        ], success=False)

        self.assertIn("reserved for gitsvn itself", result.stderr)
        # Python may cache its imports, but source and configuration stay untouched.
        self.assertEqual({path: content for path, content in self.tree_contents(program).items()
                          if "__pycache__" not in Path(path).parts}, program_before)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.read("story.txt"), "pending work must survive reserved branch\n")

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

    def test_status_shows_branch_changes_and_excludes_unrelated_files(self):
        self.write("later.txt", "tracked fixture\n")
        self.svn("add", "later.txt")
        self.svn("propset", "svn:ignore", "*.tmp", ".")
        self.svn("commit", "-m", "status fixture")
        self.new_branch()
        self.write("story.txt", "branch edit\n")
        self.write("newdir/added.txt", "new branch file\n")
        self.cli("add", "newdir")
        self.svn("delete", "removed.txt")
        self.svn("propset", "custom:flag", "branch property", "properties.txt")
        self.svn("changelist", "ignore-on-commit", "ignored.txt")
        self.write("ignored.txt", "private edit\n")
        self.write("scratch.txt", "unversioned file\n")
        self.write("build.tmp", "SVN-ignored file\n")
        self.commit("saved branch changes")
        # New tracked edits belong to the active branch before they are committed.
        self.write("later.txt", "unsaved tracked edit\n")
        state_before = self.tree_contents(self.state)
        patches_before = self.tree_contents(self.patches)
        status_before = ET.canonicalize(self.svn("status", "--xml", "--no-ignore").stdout)
        files_before = self.tree_contents(self.wc)

        result = self.cli("status")

        self.assertEqual(result.stdout.splitlines()[0], "On branch 18814")
        self.assertEqual(set(result.stdout.splitlines()[1:]),
                         {"A  newdir", "A  newdir/added.txt", "D  removed.txt",
                          "M  story.txt", "M  properties.txt", "M  later.txt"})
        self.assertNotIn("\x1b", result.stdout)
        output = io.StringIO()
        with patch("gitsvn.inspection.ansi_output") as terminal, patch("gitsvn.cli.sys.stdout", output):
            terminal.return_value.__enter__.return_value = True
            GitSvn(self.wc, self.patches, self.state).status()
        self.assertIn("\x1b[32mA\x1b[0m  \x1b[37mnewdir/added.txt\x1b[0m", output.getvalue())
        self.assertIn("\x1b[33mM\x1b[0m  \x1b[37mstory.txt\x1b[0m", output.getvalue())
        self.assertIn("\x1b[33mM\x1b[0m  \x1b[31mlater.txt\x1b[0m", output.getvalue())
        self.assertIn("\x1b[31mD\x1b[0m  \x1b[37mremoved.txt\x1b[0m", output.getvalue())
        self.assertNotIn("(*)", output.getvalue())
        self.assertNotIn("changes not included", output.getvalue())
        self.assertNotIn("ignored.txt", output.getvalue())
        self.assertEqual(self.tree_contents(self.state), state_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(ET.canonicalize(self.svn("status", "--xml", "--no-ignore").stdout),
                         status_before)
        self.assertEqual(self.tree_contents(self.wc), files_before)

    def test_status_follows_current_branch_when_switching(self):
        self.configure(autosave=False)
        self.new_branch()
        self.write("story.txt", "first branch edit\n")
        self.commit("first branch work")
        self.svn("changelist", "ignore-on-commit", "ignored.txt")
        self.write("ignored.txt", "private edit stays across branches\n")
        self.write("scratch.txt", "unversioned file stays across branches\n")
        self.new_branch("19999")
        self.write("properties.txt", "second branch edit\n")
        self.cli("commit", "-m", "second branch work")

        self.assertEqual(self.cli("status").stdout, "On branch 19999\nM  properties.txt\n")
        self.cli("switch", "18814")
        self.assertEqual(self.cli("status").stdout, "On branch 18814\nM  story.txt\n")
        self.cli("switch", "trunk")
        self.assertEqual(self.cli("status").stdout,
                         "On branch trunk\nNo tracked branch changes.\n")
        self.assertEqual(self.read("ignored.txt"), "private edit stays across branches\n")
        self.assertEqual(self.read("scratch.txt"), "unversioned file stays across branches\n")

    def test_status_keeps_missing_and_unsupported_snapshot_changes_visible(self):
        self.new_branch()
        (self.wc / "removed.txt").unlink()
        (self.wc / "binary.dat").write_bytes(b"\x00binary data\xff")
        self.cli("add", "binary.dat")
        self.svn("copy", "story.txt", "copied.txt")
        state_before = self.tree_contents(self.state)
        patches_before = self.tree_contents(self.patches)

        result = self.cli("status")

        self.assertEqual(set(result.stdout.splitlines()[1:]),
                         {"!  removed.txt", "A  binary.dat", "A  copied.txt"})
        self.assertEqual(self.tree_contents(self.state), state_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        output = io.StringIO()
        with patch("gitsvn.inspection.ansi_output") as terminal, patch("gitsvn.cli.sys.stdout", output):
            terminal.return_value.__enter__.return_value = True
            GitSvn(self.wc, self.patches, self.state).status()
        self.assertIn("\x1b[32mA\x1b[0m  \x1b[31mbinary.dat\x1b[0m", output.getvalue())
        self.assertIn("\x1b[31m!\x1b[0m  \x1b[31mremoved.txt\x1b[0m", output.getvalue())

    def test_status_marks_every_visible_change_without_a_recorded_head(self):
        self.new_branch()
        self.write("story.txt", "saved work\n")
        self.commit("existing snapshot")
        state_path = self.state / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        del state["heads"]["18814"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.write("new.txt", "new work\n")
        self.cli("add", "new.txt")
        self.svn("delete", "removed.txt")
        state_before = self.tree_contents(self.state)
        patches_before = self.tree_contents(self.patches)

        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nA  new.txt\nD  removed.txt\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), {"new.txt", "removed.txt", "story.txt"})
        self.assertEqual(self.tree_contents(self.state), state_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)

    def test_status_marks_further_edits_and_new_changes_until_committed(self):
        self.new_branch()
        self.write("story.txt", "saved work\n")
        self.commit("first work")
        self.assertEqual(self.cli("status").stdout, "On branch 18814\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), set())
        self.write("story.txt", "further work\n")
        self.write("new.txt", "new work\n")
        self.cli("add", "new.txt")

        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nA  new.txt\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), {"new.txt", "story.txt"})
        self.commit("include further work")
        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nA  new.txt\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), set())
        self.svn("revert", "story.txt")
        self.assertEqual(self.cli("status").stdout, "On branch 18814\nA  new.txt\n")
        self.svn("revert", "new.txt")
        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nNo tracked branch changes.\n")

    def test_status_compares_selected_head_despite_newer_recovery_autosave(self):
        self.new_branch()
        self.write("story.txt", "first version\n")
        first = self.commit("first work")
        self.write("story.txt", "recovery version\n")
        self.cli("revert", "18814", first)
        self.assertNotEqual(self.snapshots("18814")[-1].stem, first)

        self.assertEqual(self.cli("status").stdout, "On branch 18814\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), set())
        self.write("story.txt", "recovery version\n")
        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), {"story.txt"})

    def test_status_compares_property_values_and_header_like_payloads(self):
        self.new_branch()
        self.write("story.txt", "++ value: original\nIndex: source text\n")
        self.svn("propset", "custom:note", "++ value: original", "properties.txt")
        self.svn("propset", "custom:dir", "original directory property", ".")
        self.commit("text and property values")
        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nM  .\nM  properties.txt\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), set())
        self.write("story.txt", "++ value: changed\nIndex: source text\n")
        self.svn("propset", "custom:note", "++ value: changed", "properties.txt")
        self.svn("propset", "custom:dir", "changed directory property", ".")

        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nM  .\nM  properties.txt\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), {".", "properties.txt", "story.txt"})

    def test_status_compares_added_files_directories_and_empty_paths(self):
        self.new_branch()
        self.write("newdir/file.txt", "saved file\n")
        self.write("newdir/empty.txt", "")
        (self.wc / "newdir/emptydir").mkdir()
        (self.wc / "newdir/plainempty").mkdir()
        self.svn("propset", "custom:flag", "saved directory property", "properties.txt")
        self.cli("add", "newdir")
        self.svn("propset", "custom:flag", "saved directory property", "newdir/emptydir")
        self.commit("new paths")
        self.assertEqual(set(self.cli("status").stdout.splitlines()[1:]),
                         {"A  newdir", "A  newdir/file.txt", "A  newdir/empty.txt",
                          "A  newdir/emptydir", "A  newdir/plainempty", "M  properties.txt"})
        self.assertEqual(self.unsaved_paths(), set())
        self.write("newdir/empty.txt", "no longer empty\n")
        self.svn("propset", "custom:flag", "changed directory property", "newdir/emptydir")

        self.assertEqual(set(self.cli("status").stdout.splitlines()[1:]),
                         {"A  newdir", "A  newdir/file.txt", "A  newdir/empty.txt",
                          "A  newdir/emptydir", "A  newdir/plainempty", "M  properties.txt"})
        self.assertEqual(self.unsaved_paths(), {"newdir", "newdir/empty.txt", "newdir/emptydir"})
        self.commit("updated new paths")
        self.assertEqual(self.unsaved_paths(), set())

    def test_status_compares_empty_deletions_and_recursive_directory_deletions(self):
        self.write("tracked/nested/file.txt", "inside directory\n")
        self.write("tracked/empty.txt", "")
        (self.wc / "tracked/emptydir").mkdir()
        self.write("emptybase.txt", "")
        (self.wc / "emptybase").mkdir()
        self.svn("add", "tracked", "emptybase.txt", "emptybase")
        self.svn("commit", "-m", "deletion fixture")
        self.new_branch()
        self.svn("delete", "tracked", "emptybase.txt", "emptybase", "removed.txt")
        self.commit("deleted paths")
        result = self.cli("status").stdout
        self.assertIn("D  tracked\n", result)
        self.assertIn("D  emptybase\n", result)
        self.assertIn("D  emptybase.txt\n", result)
        self.assertIn("D  removed.txt\n", result)
        self.assertEqual(self.unsaved_paths(), set())

        self.svn("revert", "--depth", "infinity", "tracked")
        self.write("tracked/nested/file.txt", "edited instead of deleted\n")
        self.assertIn("M  tracked/nested/file.txt\n", self.cli("status").stdout)
        self.assertEqual(self.unsaved_paths(), {"tracked/nested/file.txt"})
        self.svn("revert", "tracked/nested/file.txt")
        self.svn("delete", "tracked/nested/file.txt", "tracked/empty.txt", "tracked/emptydir")
        self.assertEqual(self.unsaved_paths(), set())

    def test_status_distinguishes_truncation_and_deletion_with_the_same_text_hunk(self):
        self.new_branch()
        self.write("story.txt", "")
        self.commit("truncate file")
        self.assertEqual(self.cli("status").stdout, "On branch 18814\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), set())
        self.svn("delete", "--force", "story.txt")
        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nD  story.txt\n")
        self.assertEqual(self.unsaved_paths(), {"story.txt"})

    def test_status_marks_parent_deletion_after_saved_child_deletion(self):
        self.write("tracked/file.txt", "tracked child\n")
        self.svn("add", "tracked")
        self.svn("commit", "-m", "parent deletion fixture")
        self.new_branch()
        self.svn("delete", "tracked/file.txt")
        self.commit("delete only the child")
        self.assertEqual(self.unsaved_paths(), set())
        self.svn("delete", "tracked")

        self.assertIn("D  tracked\n", self.cli("status").stdout)
        self.assertIn("tracked", self.unsaved_paths())

    def test_status_compares_truncated_files_and_embedded_empty_file_properties(self):
        self.new_branch()
        self.write("story.txt", "")
        self.svn("propset", "custom:flag", "saved empty-file property", "story.txt")
        snapshot = self.commit("empty modified file and property")
        snapshot_path = self.patches / "18814" / f"{snapshot}.patch"
        # Older snapshots may carry properties only in the metadata sidecar.
        patch_bytes = snapshot_path.read_bytes()
        snapshot_path.write_bytes(patch_bytes.split(b"Property changes on: ", 1)[0])
        self.assertEqual(self.cli("status").stdout, "On branch 18814\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), set())
        self.svn("propset", "custom:flag", "changed empty-file property", "story.txt")
        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), {"story.txt"})

    def test_status_normalizes_revision_labels_without_ignoring_line_ending_edits(self):
        self.new_branch()
        self.write("story.txt", "saved change\n")
        snapshot = self.commit("saved change")
        snapshot_path = self.patches / "18814" / f"{snapshot}.patch"
        snapshot_path.write_bytes(snapshot_path.read_bytes().replace(b"(revision 1)", b"(revision 42)"))
        self.assertEqual(self.cli("status").stdout, "On branch 18814\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), set())
        content = (self.wc / "story.txt").read_bytes()
        (self.wc / "story.txt").write_bytes(content.replace(b"\r\n", b"\n")
                                           if b"\r\n" in content else content.replace(b"\n", b"\r\n"))
        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), {"story.txt"})

    def test_status_compares_legacy_patches_without_metadata(self):
        self.new_branch()
        self.write("story.txt", "legacy edit\n")
        self.write("newdir/new.txt", "legacy addition\n")
        self.write("empty.txt", "")
        self.cli("add", "newdir", "empty.txt")
        self.svn("delete", "removed.txt")
        self.svn("propset", "custom:flag", "legacy property", "properties.txt")
        snapshot = self.commit("legacy changes")
        snapshot_path = self.patches / "18814" / f"{snapshot}.patch"
        snapshot_path.with_suffix(".json").unlink()
        snapshot_path.write_bytes(b"\xef\xbb\xbf" + snapshot_path.read_bytes())
        self.assertEqual(self.unsaved_paths(), set())
        self.write("story.txt", "further legacy edit\n")
        self.svn("propset", "custom:flag", "new property", "properties.txt")
        result = self.cli("status").stdout
        self.assertIn("M  story.txt\n", result)
        self.assertIn("M  properties.txt\n", result)
        self.assertIn("A  newdir\n", result)
        self.assertIn("A  empty.txt\n", result)
        self.assertEqual(self.unsaved_paths(), {"story.txt", "properties.txt"})

    def test_status_compares_recursive_legacy_deletion_without_parent_metadata(self):
        self.write("tracked/nested/file.txt", "legacy directory\n")
        self.svn("add", "tracked")
        self.svn("commit", "-m", "legacy deletion fixture")
        self.new_branch()
        self.svn("delete", "tracked")
        snapshot = self.commit("legacy directory deletion")
        (self.patches / "18814" / f"{snapshot}.json").unlink()
        self.assertEqual(self.unsaved_paths(), set())

    def test_status_reads_head_once_and_does_not_capture_unsupported_changes(self):
        self.new_branch()
        self.write("story.txt", "saved work\n")
        self.commit("saved work")
        self.svn("copy", "story.txt", "copied.txt")
        app = GitSvn(self.wc, self.patches, self.state)
        output = io.StringIO()
        with patch.object(app, "load_snapshot", wraps=app.load_snapshot) as load, \
                patch.object(app, "capture", side_effect=AssertionError("status cannot capture")), \
                patch("gitsvn.cli.sys.stdout", output):
            app.status()
        load.assert_called_once()
        self.assertEqual(output.getvalue(),
                         "On branch 18814\nA  copied.txt\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), {"copied.txt"})

    def test_status_marks_binary_edits_and_data_kept_inside_saved_deletions(self):
        self.new_branch()
        self.write("story.txt", "saved text\n")
        self.svn("delete", "removed.txt")
        self.commit("saved text and deletion")
        (self.wc / "story.txt").write_bytes(b"\x00binary data\xff")
        self.write("removed.txt", "local data inside deleted path\n")
        files_before = self.tree_contents(self.wc)
        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nD  removed.txt\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), {"removed.txt", "story.txt"})
        self.assertEqual(self.tree_contents(self.wc), files_before)

    def test_status_keeps_text_and_property_conflicts_visible_and_unsaved(self):
        self.svn("propset", "custom:flag", "base property", "properties.txt")
        self.svn("commit", "-m", "property fixture")
        self.new_branch()
        self.write("story.txt", "local text\n")
        self.svn("propset", "custom:flag", "local property", "properties.txt")
        upstream = self.root / "upstream"
        self.run_process(["svn", "checkout", self.repo_url, str(upstream)])
        (upstream / "story.txt").write_text("upstream text\n", encoding="utf-8")
        self.run_process(["svn", "propset", "custom:flag", "upstream property",
                          "properties.txt"], cwd=upstream)
        self.run_process(["svn", "commit", "-m", "upstream changes"], cwd=upstream)
        self.cli("pull", success=False)
        state_before = self.tree_contents(self.state)
        patches_before = self.tree_contents(self.patches)
        status_before = ET.canonicalize(self.svn("status", "--xml").stdout)
        files_before = self.tree_contents(self.wc)

        self.assertEqual(self.cli("status").stdout,
                         "On branch 18814\nC  properties.txt\nC  story.txt\n")
        self.assertEqual(self.unsaved_paths(), {"properties.txt", "story.txt"})
        self.assertEqual(self.tree_contents(self.state), state_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(ET.canonicalize(self.svn("status", "--xml").stdout), status_before)
        self.assertEqual(self.tree_contents(self.wc), files_before)

    def test_status_excludes_external_working_copy_changes(self):
        self.write("shared/file.txt", "external fixture\n")
        self.svn("add", "shared")
        self.svn("propset", "svn:externals", "^/shared external", ".")
        self.svn("commit", "-m", "external fixture")
        self.svn("update")
        self.new_branch()
        self.write("story.txt", "saved work\n")
        self.commit("saved work")
        self.write("external/file.txt", "external edit\n")
        files_before = self.tree_contents(self.wc)
        self.assertEqual(self.cli("status").stdout, "On branch 18814\nM  story.txt\n")
        self.assertEqual(self.unsaved_paths(), set())
        self.assertEqual(self.tree_contents(self.wc), files_before)

    def test_status_reports_invalid_recorded_head_without_using_newest_snapshot(self):
        self.new_branch()
        self.write("story.txt", "saved work\n")
        snapshot = self.commit("saved work")
        state_path = self.state / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for head in ("missing-head", "../escape", None, 123):
            with self.subTest(head=head):
                state["heads"]["18814"] = head
                state_path.write_text(json.dumps(state), encoding="utf-8")
                state_before = self.tree_contents(self.state)
                patches_before = self.tree_contents(self.patches)
                result = self.cli("status", success=False)
                self.assertIn("Cannot compare status with current head", result.stderr)
                self.assertEqual(self.tree_contents(self.state), state_before)
                self.assertEqual(self.tree_contents(self.patches), patches_before)
        state["heads"]["18814"] = snapshot
        state_path.write_text(json.dumps(state), encoding="utf-8")
        snapshot_path = self.patches / "18814" / f"{snapshot}.patch"
        for corrupt in ("metadata", "patch", "missing patch"):
            with self.subTest(corrupt=corrupt):
                if corrupt == "metadata":
                    snapshot_path.with_suffix(".json").write_text("{}", encoding="utf-8")
                elif corrupt == "patch":
                    snapshot_path.with_suffix(".json").unlink()
                    snapshot_path.write_bytes(b"not an SVN patch\n")
                else:
                    snapshot_path.unlink()
                result = self.cli("status", success=False)
                self.assertIn("Cannot compare status with current head", result.stderr)
        self.svn("revert", "story.txt")
        self.cli("status", success=False)

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

    def test_switch_picker_restores_selected_branch_and_autosaves(self):
        self.new_branch()
        self.write("story.txt", "saved branch work\n")
        self.commit("saved work")
        self.cli("switch", "trunk")
        self.write("story.txt", "unsaved trunk work\n")

        result = self.cli("switch", input="1\n")

        self.assertIn("18814", result.stdout)
        self.assertIn("* trunk", result.stdout)
        self.assertIn("On branch 18814", result.stdout)
        self.assertEqual(self.read("story.txt"), "saved branch work\n")
        self.cli("switch", "trunk")
        self.assertEqual(self.read("story.txt"), "unsaved trunk work\n")

    def test_switch_picker_respects_disabled_autosave(self):
        self.configure(autosave=False)
        self.new_branch()
        self.write("story.txt", "saved branch work\n")
        self.commit("saved work")
        self.cli("switch", "trunk")
        self.write("story.txt", "uncommitted trunk work\n")
        patches_before = self.tree_contents(self.patches)

        self.cli("switch", input="1\n")

        self.assertEqual(self.read("story.txt"), "saved branch work\n")
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(json.loads((self.state / "state.json").read_text(encoding="utf-8"))
                         ["branch"], "18814")

    def test_switch_picker_includes_trunk_before_it_has_snapshots(self):
        self.configure(autosave=False)
        self.new_branch()
        self.assertFalse((self.patches / "trunk").exists())

        result = self.cli("switch", input="2\n")

        self.assertIn("trunk", result.stdout)
        self.assertIn("On branch trunk", result.stdout)
        self.assertEqual(self.read("story.txt"), "original\n")
        self.assertEqual(len(self.snapshots("trunk")), 1)

    def test_switch_picker_cancellation_and_current_branch_preserve_work(self):
        self.new_branch()
        self.cli("branch", "19999")
        self.write("story.txt", "pending work must survive the picker\n")
        # Unrelated folders and finalized patches must not appear as branches.
        (self.patches / "empty-folder").mkdir()
        invalid = self.patches / "bad name"
        invalid.mkdir()
        (invalid / "unrelated.patch").write_bytes(b"")
        (self.patches / "export.patch").write_bytes(b"")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        status_before = ET.canonicalize(self.svn("status", "--xml").stdout)

        for choice in ("q\n", "\n", "", "1\n"):
            with self.subTest(choice=choice):
                result = self.cli("switch", input=choice)
                self.assertIn("* 18814", result.stdout)
                self.assertIn("19999", result.stdout)
                self.assertIn("trunk", result.stdout)
                self.assertNotIn("empty-folder", result.stdout)
                self.assertNotIn("bad name", result.stdout)
                self.assertNotIn("export.patch", result.stdout)
                self.assertIn("Already on branch" if choice == "1\n" else "Switch cancelled",
                              result.stdout)
                self.assertEqual(self.tree_contents(self.patches), patches_before)
                self.assertEqual(self.tree_contents(self.state), state_before)
                self.assertEqual(self.read("story.txt"), "pending work must survive the picker\n")
                self.assertEqual(ET.canonicalize(self.svn("status", "--xml").stdout), status_before)

    def test_switch_picker_invalid_choice_preserves_work(self):
        self.new_branch()
        self.write("story.txt", "pending work must survive invalid choice\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)

        for choice in ("0\n", "99\n", "invalid\n"):
            with self.subTest(choice=choice):
                result = self.cli("switch", input=choice, success=False)
                self.assertIn("Choose a branch number", result.stderr)
                self.assertEqual(self.tree_contents(self.patches), patches_before)
                self.assertEqual(self.tree_contents(self.state), state_before)
                self.assertEqual(self.read("story.txt"), "pending work must survive invalid choice\n")

    def test_switch_autosave_defaults_true_when_config_key_missing(self):
        self.configure()
        self.cli("branch", "18814")
        self.write("story.txt", "default autosaved trunk work\n")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "original\n")
        self.assertTrue(any("default autosaved trunk work" in path.read_text(encoding="utf-8")
                            for path in self.snapshots("trunk")))
        self.cli("switch", "trunk")
        self.assertEqual(self.read("story.txt"), "default autosaved trunk work\n")

    def test_config_paths_support_commands_from_outside_working_copy(self):
        self.configure(svn_root=str(self.wc), patch_root=str(self.patches),
                       state_dir=".", autosave=False)
        config_path = self.state / "config.json"
        self.configured_cli(config_path, "branch", "trunk")
        self.configured_cli(config_path, "branch", "18814")
        self.configured_cli(config_path, "switch", "18814")
        relative = "nested/configured.txt"
        self.write(relative, "configured working copy addition\n")
        self.configured_cli(config_path, "add", "nested")
        self.configured_cli(config_path, "commit", "-m", "Configured paths")
        saved = self.snapshots("18814")[-1]
        self.assertIn(f"Index: {relative}\n", saved.read_text(encoding="utf-8"))
        self.configured_cli(config_path, "finalize", "Configured export")
        name = datetime.now().strftime("%Y_%m_%d") + "_Configured_export_BalcarM.patch"
        self.assertEqual((self.patches / name).read_bytes(),
                         self.snapshots("18814")[-1].read_bytes())
        history_before = self.tree_contents(self.patches)
        self.configured_cli(config_path, "switch", "trunk")
        self.assertFalse((self.wc / relative).exists())
        self.configured_cli(config_path, "switch", "18814")
        self.assertEqual(self.read(relative), "configured working copy addition\n")
        self.assertEqual(self.tree_contents(self.patches), history_before)
        self.assertEqual(json.loads((self.state / "state.json").read_text(encoding="utf-8"))
                         ["branch"], "18814")

    def test_config_relative_paths_resolve_from_config_directory(self):
        config_dir = self.root / "settings"
        config_dir.mkdir()
        config_path = config_dir / "config.json"
        config_path.write_text(json.dumps({
            "svn_root": "../wc", "patch_root": "../patches", "state_dir": "saved-state",
        }), encoding="utf-8")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()

        self.configured_cli(config_path, "branch", "18814", cwd=elsewhere)

        self.assertEqual(len(self.snapshots("18814")), 1)
        state_path = config_dir / "saved-state" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(Path(state["svn_root"]), self.wc)
        self.assertEqual(Path(state["patch_root"]), self.patches)
        self.assertFalse((elsewhere / "saved-state").exists())
        self.assertFalse(self.state.exists())

    def test_config_defaults_state_to_selected_config_directory(self):
        self.configure(svn_root=str(self.wc), patch_root=str(self.patches))
        config_path = self.state / "config.json"

        self.configured_cli(config_path, "branch", "18814")

        self.assertTrue((self.state / "state.json").is_file())
        self.assertEqual(len(self.snapshots("18814")), 1)

    def test_state_dir_discovers_config_and_overrides_configured_state_dir(self):
        other_state = self.root / "other-state"
        self.configure(svn_root=str(self.wc), patch_root=str(self.patches),
                       state_dir=str(other_state))

        self.run_process([sys.executable, str(MAIN), "--state-dir", str(self.state),
                          "branch", "18814"])

        self.assertTrue((self.state / "state.json").is_file())
        self.assertFalse(other_state.exists())
        self.assertEqual(len(self.snapshots("18814")), 1)

    def test_default_config_is_loaded_from_program_directory(self):
        program_dir = self.root / "program"
        config_dir = program_dir / ".gitsvn"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.json"
        config_path.write_text(json.dumps({
            "svn_root": str(self.wc), "patch_root": str(self.patches),
            "state_dir": ".", "autosave": False,
        }), encoding="utf-8")

        with patch("gitsvn.config.program_directory", return_value=program_dir):
            app = GitSvn()
            app.verify_working_copy()
            app.create_branch("18814")

        self.assertEqual(app.config_path, config_path)
        self.assertEqual(app.state_dir, config_dir)
        self.assertFalse(app.config["autosave"])
        self.assertEqual(len(self.snapshots("18814")), 1)
        self.assertTrue((config_dir / "state.json").is_file())

    def test_cli_paths_override_config_and_remain_relative_to_command_directory(self):
        self.configure(svn_root=str(self.wc), patch_root=str(self.patches),
                       state_dir=".")
        config_path = self.state / "config.json"
        self.configured_cli(config_path, "branch", "18814")
        state_before = self.tree_contents(self.state)
        patches_before = self.tree_contents(self.patches)
        alternate_wc = self.root / "alternate-wc"
        self.run_process(["svn", "checkout", self.repo_url, str(alternate_wc)])
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        alternate_state_dir = self.root / "alternate-state"
        alternate_state_dir.mkdir()
        (alternate_state_dir / "config.json").write_text("{", encoding="utf-8")

        self.configured_cli(config_path, "--svn-root", "../alternate-wc",
                            "--patch-root", "../alternate-patches",
                            "--state-dir", "../alternate-state",
                            "branch", "19999", cwd=elsewhere)

        alternate_state = json.loads((self.root / "alternate-state" / "state.json")
                                     .read_text(encoding="utf-8"))
        self.assertEqual(Path(alternate_state["svn_root"]), alternate_wc)
        self.assertEqual(Path(alternate_state["patch_root"]), self.root / "alternate-patches")
        self.assertEqual(len(list((self.root / "alternate-patches" / "19999")
                                 .glob("*.patch"))), 1)
        self.assertEqual(self.tree_contents(self.state), state_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)

    def test_config_workspace_change_requires_separate_state_directory(self):
        self.configure(svn_root=str(self.wc), patch_root=str(self.patches))
        config_path = self.state / "config.json"
        self.configured_cli(config_path, "branch", "18814")
        state_before = self.tree_contents(self.state)
        patches_before = self.tree_contents(self.patches)
        alternate_patches = self.root / "alternate-patches"

        result = self.configured_cli(config_path, "--patch-root", str(alternate_patches),
                                     "branch", "19999", success=False)

        self.assertIn("separate --state-dir", result.stderr)
        self.assertEqual(self.tree_contents(self.state), state_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertFalse(alternate_patches.exists())

    def test_invalid_config_paths_are_rejected_before_mutation(self):
        self.new_branch()
        self.write("story.txt", "keep edits when configured paths are invalid\n")
        config_path = self.root / "config.json"
        settings = {"svn_root": str(self.wc), "patch_root": str(self.patches),
                    "state_dir": str(self.state)}
        status_before = ET.canonicalize(self.svn("status", "--xml").stdout)
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        for key in ("svn_root", "patch_root", "state_dir"):
            for value in ("", "   ", None, 0, []):
                with self.subTest(key=key, value=value):
                    config_path.write_text(json.dumps({**settings, key: value}),
                                           encoding="utf-8")
                    config_before = config_path.read_bytes()

                    result = self.configured_cli(config_path, "switch", "trunk", success=False)

                    self.assertIn("config", result.stderr.lower())
                    self.assertEqual(config_path.read_bytes(), config_before)
                    self.assertEqual(self.tree_contents(self.patches), patches_before)
                    self.assertEqual(self.tree_contents(self.state), state_before)
                    self.assertEqual(ET.canonicalize(self.svn("status", "--xml").stdout),
                                     status_before)
                    self.assertEqual(self.read("story.txt"),
                                     "keep edits when configured paths are invalid\n")

    def test_config_storage_inside_working_copy_is_rejected(self):
        config_path = self.root / "config.json"
        settings = {"svn_root": str(self.wc), "patch_root": str(self.patches),
                    "state_dir": str(self.state)}
        for key in ("patch_root", "state_dir"):
            with self.subTest(key=key):
                forbidden = self.wc / key
                config_path.write_text(json.dumps({**settings, key: str(forbidden)}),
                                       encoding="utf-8")

                result = self.configured_cli(config_path, "branch", "18814", success=False)

                self.assertIn("outside the SVN working copy", result.stderr)
                self.assertFalse(forbidden.exists())
                self.assertFalse(self.patches.exists())
                self.assertFalse(self.state.exists())

    def test_help_is_available_with_invalid_config(self):
        config_path = self.root / "invalid-config.json"
        config_path.write_text("{", encoding="utf-8")

        result = self.configured_cli(config_path, "--help")

        self.assertIn("--config", result.stdout)
        self.assertIn("--svn-root", result.stdout)
        self.assertFalse(self.patches.exists())
        self.assertFalse(self.state.exists())

    def test_explicit_missing_config_is_rejected_before_mutation(self):
        config_path = self.root / "missing-config.json"
        working_copy_before = self.tree_contents(self.wc)

        result = self.configured_cli(config_path, "--svn-root", str(self.wc),
                                     "--patch-root", str(self.patches),
                                     "--state-dir", str(self.state),
                                     "branch", "18814", success=False)

        self.assertIn("config", result.stderr.lower())
        self.assertIn(str(config_path), result.stderr)
        self.assertEqual(self.tree_contents(self.wc), working_copy_before)
        self.assertFalse(config_path.exists())
        self.assertFalse(self.patches.exists())
        self.assertFalse(self.state.exists())

    def test_switch_autosave_false_preserves_history_and_saved_head(self):
        self.configure(autosave=False)
        self.cli("branch", "trunk")
        self.new_branch()
        self.write("story.txt", "explicitly saved branch work\n")
        self.commit("saved work")
        history_before = self.tree_contents(self.patches)
        heads_before = json.loads((self.state / "state.json").read_text(encoding="utf-8"))["heads"]

        self.write("story.txt", "uncommitted work to discard\n")
        self.cli("switch", "trunk")
        self.assertEqual(self.read("story.txt"), "original\n")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "explicitly saved branch work\n")

        # A manually cleaned working copy also must not replace the saved head.
        self.svn("revert", "story.txt")
        self.cli("switch", "trunk")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "explicitly saved branch work\n")
        self.assertEqual(self.tree_contents(self.patches), history_before)
        self.assertEqual(json.loads((self.state / "state.json").read_text(encoding="utf-8"))["heads"],
                         heads_before)
        self.assertEqual(set(self.tree_contents(self.state)), {"config.json", "state.json"})

    def test_switch_autosave_false_preserves_ignored_and_untracked_files(self):
        self.configure(autosave=False)
        self.cli("branch", "trunk")
        self.new_branch()
        self.svn("changelist", "ignore-on-commit", "ignored.txt")
        self.write("ignored.txt", "private ignored edit\n")
        self.write("story.txt", "uncommitted branch edit\n")
        self.write("newdir/added.txt", "uncommitted addition\n")
        self.cli("add", "newdir")
        self.write("newdir/scratch.txt", "untracked scratch\n")
        history_before = self.tree_contents(self.patches)

        self.cli("switch", "trunk")

        self.assertEqual(self.read("story.txt"), "original\n")
        self.assertEqual(self.read("ignored.txt"), "private ignored edit\n")
        self.assertEqual(self.read("newdir/scratch.txt"), "untracked scratch\n")
        self.assertFalse((self.wc / "newdir/added.txt").exists())
        self.assertEqual(self.tree_contents(self.patches), history_before)
        self.assertEqual(set(self.tree_contents(self.state)), {"config.json", "state.json"})

    def test_invalid_autosave_config_rejected_before_mutation(self):
        self.new_branch()
        self.write("story.txt", "keep edits when config is invalid\n")
        for contents in ("{", "[]", "null", '{"autosave": "false"}',
                         '{"autosave": 0}', '{"autosave": null}'):
            with self.subTest(contents=contents):
                (self.state / "config.json").write_text(contents, encoding="utf-8")
                patches_before = self.tree_contents(self.patches)
                state_before = self.tree_contents(self.state)
                status_before = ET.canonicalize(self.svn("status", "--xml").stdout)

                result = self.cli("switch", "trunk", success=False)

                self.assertIn("config", (result.stdout + result.stderr).lower())
                self.assertEqual(self.read("story.txt"), "keep edits when config is invalid\n")
                self.assertEqual(self.tree_contents(self.patches), patches_before)
                self.assertEqual(self.tree_contents(self.state), state_before)
                self.assertEqual(ET.canonicalize(self.svn("status", "--xml").stdout), status_before)

    def test_switch_autosave_false_failure_restores_work_without_history(self):
        self.configure(autosave=False)
        self.cli("branch", "trunk")
        self.new_branch()
        self.write("story.txt", "saved branch change\n")
        self.commit("branch story")
        self.cli("switch", "trunk")
        upstream = self.root / "upstream"
        self.run_process(["svn", "checkout", self.repo_url, str(upstream)])
        (upstream / "story.txt").write_text("upstream change\n", encoding="utf-8")
        self.run_process(["svn", "commit", "-m", "upstream edit"], cwd=upstream)
        self.svn("update")

        self.write("story.txt", "outgoing dirty story\n")
        self.write("empty.txt", "")
        (self.wc / "emptydir").mkdir()
        self.write("newdir/added.txt", "outgoing addition\n")
        self.cli("add", "empty.txt", "emptydir", "newdir")
        self.write("newdir/scratch.txt", "outgoing untracked data\n")
        self.svn("delete", "removed.txt")
        self.svn("propset", "custom:flag", "outgoing property", "properties.txt")
        self.svn("changelist", "ignore-on-commit", "ignored.txt")
        self.write("ignored.txt", "private ignored edit\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        status_before = ET.canonicalize(self.svn("status", "--xml").stdout)

        result = self.cli("switch", "18814", success=False)

        self.assertIn("conflict", (result.stdout + result.stderr).lower())
        self.assertEqual(self.read("story.txt"), "outgoing dirty story\n")
        self.assertEqual((self.wc / "empty.txt").read_bytes(), b"")
        self.assertTrue((self.wc / "emptydir").is_dir())
        self.assertEqual(self.read("newdir/added.txt"), "outgoing addition\n")
        self.assertEqual(self.read("newdir/scratch.txt"), "outgoing untracked data\n")
        self.assertFalse((self.wc / "removed.txt").exists())
        self.assertEqual(self.svn("propget", "custom:flag", "properties.txt").stdout.strip(),
                         "outgoing property")
        self.assertEqual(self.read("ignored.txt"), "private ignored edit\n")
        self.assertEqual(ET.canonicalize(self.svn("status", "--xml").stdout), status_before)
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)
        self.assertFalse(list(self.wc.rglob("*.svnpatch.rej")))

    def test_autosave_false_keeps_explicit_revert_and_pull_backups(self):
        self.configure(autosave=False)
        self.new_branch()
        self.write("story.txt", "saved branch work\n")
        saved = self.commit("saved work")
        count_before = len(self.snapshots("18814"))
        self.write("story.txt", "unsaved work before explicit revert\n")

        self.cli("revert", "18814", saved)

        self.assertEqual(self.read("story.txt"), "saved branch work\n")
        self.assertEqual(len(self.snapshots("18814")), count_before + 1)
        self.assertIn("unsaved work before explicit revert",
                      self.snapshots("18814")[-1].read_text(encoding="utf-8"))
        self.write("story.txt", "unsaved work before pull\n")

        self.cli("pull")

        self.assertEqual(self.read("story.txt"), "unsaved work before pull\n")
        self.assertEqual(len(self.snapshots("18814")), count_before + 2)
        self.assertIn("unsaved work before pull",
                      self.snapshots("18814")[-1].read_text(encoding="utf-8"))

    def test_switch_autosave_false_interrupted_rollback_keeps_recovery_snapshot(self):
        self.configure(autosave=False)
        self.cli("branch", "18814")
        self.write("story.txt", "outgoing work requiring recovery\n")
        app = GitSvn(self.wc, self.patches, self.state)
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)

        with patch.object(app, "apply_snapshot", side_effect=[
                GitSvnError("target restore failed"), KeyboardInterrupt("rollback interrupted")]):
            with self.assertRaises(GitSvnError) as caught:
                app.switch("18814")

        recovery = list(self.state.glob("switch-*.patch"))
        self.assertEqual(len(recovery), 1)
        recovery_path = recovery[0]
        self.assertIn("Rollback also failed", str(caught.exception))
        self.assertIn(f"Recovery snapshot: {recovery_path}", str(caught.exception))
        self.assertIn("+outgoing work requiring recovery",
                      recovery_path.read_text(encoding="utf-8"))
        self.assertTrue(recovery_path.with_suffix(".json").is_file())
        recovery_snapshot = app.load_snapshot(recovery_path)
        self.assertEqual([entry["path"] for entry in recovery_snapshot.entries], ["story.txt"])
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(app.state, json.loads(state_before["state.json"]))
        state_after = self.tree_contents(self.state)
        for name, contents in state_before.items():
            self.assertEqual(state_after[name], contents)
        self.assertEqual(set(state_after), set(state_before) | {
            recovery_path.name, recovery_path.with_suffix(".json").name})

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
        for choice in ("q\n", "\n", ""):
            with self.subTest(choice=choice):
                result = self.cli("revert", "18814", input=choice)
                self.assertIn(snapshot, result.stdout)
                self.assertIn("visible snapshot message", result.stdout)
                self.assertIn("Restore cancelled", result.stdout)
                self.assertEqual(self.read("story.txt"), "still unsaved\n")
                self.assertEqual(self.tree_contents(self.patches), patches_before)
                self.assertEqual(self.tree_contents(self.state), state_before)

    def test_revert_picker_selected_snapshot_is_head_despite_newer_autosave(self):
        self.configure(autosave=False)
        self.new_branch()
        self.write("story.txt", "first saved version\n")
        first = self.commit("first version")
        self.write("story.txt", "second saved version\n")
        self.commit("second version")
        self.write("story.txt", "unsaved work before reverting\n")
        app = GitSvn(self.wc, self.patches, self.state)
        history = app.history("18814")
        choice = next(index for index, path in enumerate(history, 1) if path.stem == first)
        snapshots_before = set(history)

        result = self.cli("revert", input=f"{choice}\n")

        self.assertIn("saved head", result.stdout)
        saved_at = datetime.strptime(first, "%Y%m%d_%H%M%S_%f")
        self.assertIn(f"{saved_at:%d.%m.%Y %H:%M:%S}", result.stdout)
        self.assertEqual(self.read("story.txt"), "first saved version\n")
        autosaves = set(self.snapshots("18814")) - snapshots_before
        self.assertEqual(len(autosaves), 1)
        autosave = autosaves.pop()
        self.assertGreater(autosave.stem, first)
        self.assertIn("+unsaved work before reverting", autosave.read_text(encoding="utf-8"))
        state = json.loads((self.state / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["heads"]["18814"], first)
        self.assertNotEqual(state["heads"]["18814"], autosave.stem)
        log = self.cli("log").stdout
        self.assertIn(f"[{first}] (current version)", log)
        self.assertEqual(log.count("(current version)"), 1)
        autosave_line = next(line for line in log.splitlines() if f"[{autosave.stem}]" in line)
        self.assertNotIn("(current version)", autosave_line)
        self.cli("switch", "trunk")
        self.cli("switch", "18814")
        self.assertEqual(self.read("story.txt"), "first saved version\n")

    def test_revert_picker_arrow_selection_and_saved_head_highlight(self):
        self.new_branch()
        self.write("story.txt", "first saved version\n")
        first = self.commit("first line\nsecond line")
        self.write("story.txt", "second saved version\n")
        self.commit("second version")
        app = GitSvn(self.wc, self.patches, self.state)
        output = io.StringIO()
        with patch("gitsvn.terminal.picker_input") as console, patch("gitsvn.cli.sys.stdout", output):
            console.return_value.__enter__.return_value = iter(["\xe0", "P", "\r"]).__next__
            app.select_restore("18814", None)
        self.assertEqual(self.read("story.txt"), "first saved version\n")
        self.assertEqual(app.state["heads"]["18814"], first)
        self.assertIn("first line second line", output.getvalue())

        # Reopening the menu selects the saved head, not the newer recovery autosave.
        self.write("story.txt", "unsaved work while browsing\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        output = io.StringIO()
        with patch("gitsvn.terminal.picker_input") as console, patch("gitsvn.cli.sys.stdout", output):
            console.return_value.__enter__.return_value = iter(["\x1b"]).__next__
            app.select_restore("18814", None)
        saved_at = datetime.strptime(first, "%Y%m%d_%H%M%S_%f")
        self.assertIn(f"\x1b[7m> (saved head) {saved_at:%d.%m.%Y %H:%M:%S} first line second line",
                      output.getvalue())
        self.assertIn("Autosave before restoring", output.getvalue())
        self.assertIn("Restore cancelled", output.getvalue())
        self.assertEqual(self.read("story.txt"), "unsaved work while browsing\n")
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)

    def test_revert_picker_invalid_choice_preserves_work(self):
        self.new_branch()
        self.write("story.txt", "unsaved work must survive invalid choice\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        for choice in ("0\n", "99\n", "invalid\n"):
            with self.subTest(choice=choice):
                result = self.cli("revert", "18814", input=choice, success=False)
                self.assertIn("Choose a snapshot number", result.stderr)
                self.assertEqual(self.read("story.txt"), "unsaved work must survive invalid choice\n")
                self.assertEqual(self.tree_contents(self.patches), patches_before)
                self.assertEqual(self.tree_contents(self.state), state_before)

    def test_revert_picker_empty_history_preserves_work(self):
        (self.patches / "empty").mkdir(parents=True)
        self.write("story.txt", "unsaved work with no snapshots\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)

        result = self.cli("revert", "empty")

        self.assertIn("No snapshots in empty", result.stdout)
        self.assertNotIn("Snapshot number", result.stdout)
        self.assertEqual(self.read("story.txt"), "unsaved work with no snapshots\n")
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
        self.assertIn("C  story.txt\n", self.cli("status").stdout)
        self.assertIn("story.txt", self.unsaved_paths())
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

    def test_finalize_captures_pending_work_with_root_relative_headers(self):
        self.new_branch()
        relative = "masa/applications/www.test/UnitTests/SViewPriorityTest.cs"
        self.write(relative, "namespace UnitTests { class SViewPriorityTest {} }\n")
        self.cli("add", "masa")
        self.write("story.txt", "pending finalized edit\n")
        self.svn("changelist", "ignore-on-commit", "ignored.txt")
        self.write("ignored.txt", "private ignored edit\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        status_before = ET.canonicalize(self.svn("status", "--xml").stdout)
        revision_before = self.svn("info", "--show-item", "revision", self.repo_url).stdout

        self.cli("finalize", "Fix priority")

        name = datetime.now().strftime("%Y_%m_%d") + "_Fix_priority_BalcarM"
        patch_path = self.patches / (name + ".patch")
        snapshot_path = self.snapshots("18814")[-1]
        self.assertRegex(snapshot_path.stem, r"^\d{8}_\d{6}_\d{6}$")
        self.assertEqual(patch_path.read_bytes(), snapshot_path.read_bytes())
        patch = patch_path.read_text(encoding="utf-8")
        self.assertIn("+pending finalized edit", patch)
        self.assertIn(f"Index: {relative}\n", patch)
        self.assertIn(f"--- {relative}\t(nonexistent)", patch)
        self.assertIn(f"+++ {relative}\t(working copy)", patch)
        self.assertNotIn("ignored.txt", patch)
        self.assertNotIn(str(self.wc), patch)
        self.assertNotIn(str(self.wc).replace("\\", "/"), patch)
        self.assertEqual(snapshot_path.with_suffix(".txt").read_text(encoding="utf-8"),
                         "Fix priority\n")
        self.assertFalse(patch_path.with_suffix(".txt").exists())
        self.assertFalse(patch_path.with_suffix(".json").exists())
        metadata = json.loads(snapshot_path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], 1)
        self.assertIn(relative, {entry["path"] for entry in metadata["entries"]})
        self.assertNotIn("ignored.txt", {entry["path"] for entry in metadata["entries"]})
        expected_state = json.loads(state_before["state.json"])
        expected_state["heads"]["18814"] = snapshot_path.stem
        self.assertEqual(json.loads((self.state / "state.json").read_text(encoding="utf-8")),
                         expected_state)
        for path, contents in patches_before.items():
            self.assertEqual(self.tree_contents(self.patches)[path], contents)
        self.assertEqual(len(self.tree_contents(self.patches)), len(patches_before) + 4)
        self.assertEqual(ET.canonicalize(self.svn("status", "--xml").stdout), status_before)
        self.assertEqual(self.read("story.txt"), "pending finalized edit\n")
        self.assertEqual(self.read("ignored.txt"), "private ignored edit\n")
        self.assertEqual(self.svn("info", "--show-item", "revision", self.repo_url).stdout,
                         revision_before)

    def test_finalize_explicit_ticket_updates_target_head_without_switching(self):
        self.new_branch()
        self.write("story.txt", "current branch pending work\n")
        branch_before = self.tree_contents(self.patches / "18814")
        state_before = self.tree_contents(self.state)
        name = datetime.now().strftime("%Y_%m_%d") + "_Other_ticket_BalcarM"
        patch_path = self.patches / (name + ".patch")
        for extension in (".txt", ".json"):
            patch_path.with_suffix(extension).write_bytes(b"unrelated existing companion")

        self.cli("finalize", "Other ticket", "19999")

        snapshots = self.snapshots("19999")
        self.assertEqual(len(snapshots), 1)
        snapshot_path = snapshots[0]
        self.assertRegex(snapshot_path.stem, r"^\d{8}_\d{6}_\d{6}$")
        self.assertEqual(patch_path.read_bytes(), snapshot_path.read_bytes())
        self.assertIn("+current branch pending work",
                      patch_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot_path.with_suffix(".txt").read_text(encoding="utf-8"),
                         "Other ticket\n")
        self.assertTrue(snapshot_path.with_suffix(".json").is_file())
        for extension in (".txt", ".json"):
            self.assertEqual(patch_path.with_suffix(extension).read_bytes(),
                             b"unrelated existing companion")
        self.assertEqual(self.tree_contents(self.patches / "18814"), branch_before)
        expected_state = json.loads(state_before["state.json"])
        expected_state["heads"]["19999"] = snapshot_path.stem
        self.assertEqual(json.loads((self.state / "state.json").read_text(encoding="utf-8")),
                         expected_state)
        self.assertEqual(self.read("story.txt"), "current branch pending work\n")

    def test_finalize_latest_exports_selected_historical_head_and_metadata(self):
        self.new_branch()
        self.write("story.txt", "")
        self.svn("propset", "custom:flag", "selected historical property", "story.txt")
        first = self.commit("historical empty file and property")
        first_path = self.patches / "18814" / (first + ".patch")
        self.write("story.txt", "newer saved work\n")
        self.commit("newer branch snapshot")
        self.cli("revert", "18814", first)
        self.write("story.txt", "pending work must not be exported\n")
        binary = b"\x00pending binary\xff"
        (self.wc / "binary.dat").write_bytes(binary)
        self.cli("add", "binary.dat")
        state_before = self.tree_contents(self.state)
        patches_before = self.tree_contents(self.patches)
        status_before = ET.canonicalize(self.svn("status", "--xml").stdout)
        self.assertEqual(json.loads((self.state / "state.json").read_text(encoding="utf-8"))
                         ["heads"]["18814"], first)

        self.cli("finalize", "Historical export", "19999", "--latest")

        name = datetime.now().strftime("%Y_%m_%d") + "_Historical_export_BalcarM"
        patch_path = self.patches / (name + ".patch")
        snapshots = self.snapshots("19999")
        self.assertEqual(len(snapshots), 1)
        snapshot_path = snapshots[0]
        self.assertRegex(snapshot_path.stem, r"^\d{8}_\d{6}_\d{6}$")
        self.assertEqual(patch_path.read_bytes(), snapshot_path.read_bytes())
        self.assertEqual(patch_path.read_bytes(), first_path.read_bytes())
        metadata = json.loads(snapshot_path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(metadata, json.loads(
            first_path.with_suffix(".json").read_text(encoding="utf-8")))
        self.assertTrue(metadata["empty_properties"])
        self.assertEqual(snapshot_path.with_suffix(".txt").read_text(encoding="utf-8"),
                         "Historical export\n")
        self.assertFalse(patch_path.with_suffix(".txt").exists())
        self.assertFalse(patch_path.with_suffix(".json").exists())
        expected_state = json.loads(state_before["state.json"])
        expected_state["heads"]["19999"] = snapshot_path.stem
        self.assertEqual(json.loads((self.state / "state.json").read_text(encoding="utf-8")),
                         expected_state)
        for path, contents in patches_before.items():
            self.assertEqual(self.tree_contents(self.patches)[path], contents)
        self.assertEqual(len(self.tree_contents(self.patches)), len(patches_before) + 4)
        self.assertEqual(ET.canonicalize(self.svn("status", "--xml").stdout), status_before)
        self.assertEqual(self.read("story.txt"), "pending work must not be exported\n")
        self.assertEqual((self.wc / "binary.dat").read_bytes(), binary)

    def test_finalize_latest_uses_newest_snapshot_when_no_head_is_saved(self):
        self.new_branch()
        self.write("story.txt", "saved newest work\n")
        newest = self.commit("newest saved snapshot")
        newest_path = self.patches / "18814" / (newest + ".patch")
        state_path = self.state / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["heads"].pop("18814")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.write("story.txt", "unsaved later work\n")
        snapshots_before = self.snapshots("18814")

        self.cli("finalize", "Newest export", "--latest")

        name = datetime.now().strftime("%Y_%m_%d") + "_Newest_export_BalcarM"
        patch_path = self.patches / (name + ".patch")
        snapshots = self.snapshots("18814")
        self.assertEqual(len(snapshots), len(snapshots_before) + 1)
        snapshot_path = snapshots[-1]
        self.assertRegex(snapshot_path.stem, r"^\d{8}_\d{6}_\d{6}$")
        self.assertEqual(patch_path.read_bytes(), snapshot_path.read_bytes())
        self.assertEqual(patch_path.read_bytes(), newest_path.read_bytes())
        self.assertFalse(patch_path.with_suffix(".txt").exists())
        self.assertFalse(patch_path.with_suffix(".json").exists())
        state["heads"]["18814"] = snapshot_path.stem
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), state)
        self.assertEqual(self.read("story.txt"), "unsaved later work\n")

    def test_finalize_collisions_preserve_every_file_and_working_state(self):
        self.new_branch()
        self.write("story.txt", "saved snapshot\n")
        self.commit("saved snapshot")
        self.write("story.txt", "pending work must survive collision\n")
        for latest in (False, True):
            with self.subTest(latest=latest):
                description = f"Collision_{latest}"
                name = datetime.now().strftime("%Y_%m_%d") + "_" + description + "_BalcarM"
                (self.patches / (name + ".patch")).write_bytes(b"existing bytes must survive")
                patches_before = self.tree_contents(self.patches)
                state_before = self.tree_contents(self.state)
                status_before = ET.canonicalize(self.svn("status", "--xml").stdout)
                args = ["finalize", description, "19999"]
                if latest:
                    args.append("--latest")

                self.cli(*args, success=False)

                self.assertEqual(self.tree_contents(self.patches), patches_before)
                self.assertEqual(self.tree_contents(self.state), state_before)
                self.assertEqual(ET.canonicalize(self.svn("status", "--xml").stdout),
                                 status_before)
                self.assertEqual(self.read("story.txt"),
                                 "pending work must survive collision\n")
                self.assertFalse((self.patches / "19999").exists())

    def test_finalize_rejects_unsafe_description_and_ticket_before_mutation(self):
        self.new_branch()
        self.write("story.txt", "pending work must survive unsafe name\n")
        patches_before = self.tree_contents(self.patches)
        state_before = self.tree_contents(self.state)
        for description in ("", "   ", "../escape", "nested/escape", "nested\\escape",
                            "bad:name", "bad*name", "bad?name", "bad|name", "nonascii_č"):
            with self.subTest(description=description):
                self.cli("finalize", description, success=False)
        for ticket in ("../escape", "..", "nested/ticket", "nested\\ticket",
                       str(self.root / "absolute")):
            with self.subTest(ticket=ticket):
                self.cli("finalize", "Safe description", ticket, success=False)
        self.assertEqual(self.tree_contents(self.patches), patches_before)
        self.assertEqual(self.tree_contents(self.state), state_before)
        self.assertEqual(self.read("story.txt"), "pending work must survive unsafe name\n")
        self.assertFalse((self.root / "escape").exists())
        self.assertFalse((self.root / "absolute").exists())


class BranchPickerTests(unittest.TestCase):
    def choose(self, keys, names=None, current="trunk", lines=24):
        output = io.StringIO()
        with patch("gitsvn.terminal.picker_input") as console, patch("gitsvn.cli.sys.stdout", output), \
                patch("gitsvn.terminal.shutil.get_terminal_size", return_value=os.terminal_size((80, lines))):
            console.return_value.__enter__.return_value = iter(keys).__next__
            selected = pick_branch(names or ["18814", "19999", "trunk"], current)
        return selected, output.getvalue()

    def test_enter_selects_current_branch(self):
        selected, output = self.choose(["\r"])
        self.assertEqual(selected, "trunk")
        self.assertIn("(current) trunk", output)
        self.assertIn("\x1b[7m> (current) trunk\x1b[0m", output)
        self.assertTrue(output.endswith("\x1b[0m\x1b[?25h"))

    def test_arrow_keys_support_windows_prefixes_and_wrap(self):
        for keys, expected in ((["\xe0", "H", "\r"], "19999"),
                               (["\x00", "P", "\r"], "18814"),
                               (["\xe0", "P", "\x00", "H", "\r"], "trunk")):
            with self.subTest(keys=keys):
                selected, _ = self.choose(keys)
                self.assertEqual(selected, expected)

    def test_escape_and_q_cancel(self):
        for key in ("\x1b", "q", "Q"):
            with self.subTest(key=key):
                selected, output = self.choose([key])
                self.assertIsNone(selected)
                self.assertTrue(output.endswith("\x1b[0m\x1b[?25h"))

    def test_long_list_scrolls_to_selected_branch(self):
        names = [f"branch-{index:02}" for index in range(35)]
        selected, output = self.choose(["\xe0", "G", "\r"], names, names[-1], lines=8)
        self.assertEqual(selected, names[0])
        self.assertIn("(current) " + names[-1], output)
        self.assertIn("> " + names[0], output)
        self.assertIn("\x1b[5A", output)
        selected, _ = self.choose(["\x00", "O", "\r"], names, names[0], lines=8)
        self.assertEqual(selected, names[-1])

    def test_keyboard_interrupt_restores_cursor(self):
        output = io.StringIO()
        with patch("gitsvn.terminal.picker_input") as console, patch("gitsvn.cli.sys.stdout", output):
            console.return_value.__enter__.return_value = iter(["\x03"]).__next__
            with self.assertRaises(KeyboardInterrupt):
                pick_branch(["18814", "trunk"], "trunk")
        self.assertTrue(output.getvalue().endswith("\x1b[0m\x1b[?25h"))


if __name__ == "__main__":
    unittest.main()
