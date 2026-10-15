"""Restore one working-copy file from the current branch's saved head."""

from datetime import datetime
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from .config import save_json
from .models import GitSvnError, Snapshot
from .terminal import pick_item


class FileRestoreMixin:
    def file_snapshot(self, snapshot, value):
        patch = bytearray()
        selected = False
        old_lines = new_lines = 0
        for raw_line in snapshot.patch.removeprefix(b"\xef\xbb\xbf").splitlines(keepends=True):
            line = raw_line.rstrip(b"\r\n")
            if not (old_lines or new_lines):
                for prefix in (b"Index: ", b"Property changes on: ", b"--- ", b"+++ "):
                    if line.startswith(prefix):
                        path = line[len(prefix):].split(b"\t", 1)[0]
                        if path != b"/dev/null":
                            selected = self.safe_path(path.decode("utf-8")) == self.safe_path(value)
                        break
            if selected:
                patch.extend(raw_line)
            if old_lines or new_lines:
                if line.startswith(b"-"):
                    old_lines -= 1
                elif line.startswith(b"+"):
                    new_lines -= 1
                elif line.startswith(b" "):
                    old_lines -= 1
                    new_lines -= 1
            else:
                hunk = re.match(br"^(?:@@|##) -\d+(?:,(\d+))? \+\d+(?:,(\d+))? (?:@@|##)", line)
                if hunk:
                    old_lines = int(hunk[1]) if hunk[1] is not None else 1
                    new_lines = int(hunk[2]) if hunk[2] is not None else 1
        target = self.safe_path(value)
        entries = [entry for entry in snapshot.entries if self.safe_path(entry["path"]) == target]
        if not entries and any(entry["kind"] == "dir" and entry["item"] == "deleted" and
                               target.is_relative_to(self.safe_path(entry["path"]))
                               for entry in snapshot.entries):
            entries = [{"path": value, "kind": "file", "item": "deleted", "empty": False}]
        properties = {path: data for path, data in snapshot.empty_properties.items()
                      if self.safe_path(path) == target}
        result = Snapshot(snapshot.name, bytes(patch), entries, properties)
        if any(self.safe_path(path) != target for path in self.patch_paths(result)):
            raise GitSvnError("The selected file patch affects another path.")
        return result

    @staticmethod
    def write_restore_snapshot(path, snapshot):
        path.write_bytes(snapshot.patch)
        save_json(path.with_suffix(".json"), {"version": 1, "entries": snapshot.entries,
                                             "empty_properties": snapshot.empty_properties})

    def restore_file(self, value=None):
        if value is None:
            changes = sorted((entry for entry in self.changes(self.entries()) if entry.kind == "file"),
                             key=lambda entry: entry.path)
            if not changes:
                print("No tracked file changes to restore.")
                return
            selected = pick_item([entry.path for entry in changes],
                                 f"Restore file to saved head on {self.branch}", "file")
            if selected is None:
                print("File restore cancelled.")
                return
            value = changes[selected].path
        if self.branch not in self.state["heads"]:
            raise GitSvnError("No saved head on this branch; use gitsvn commit first.")
        head = self.state["heads"][self.branch]
        head_path = next((path for path in self.history(self.branch) if path.stem == head), None)
        if head_path is None:
            raise GitSvnError(f"Snapshot does not exist: {self.branch}/{head}")
        snapshot = self.load_snapshot(head_path)
        path = Path(value)
        if path.is_absolute():
            try:
                value = str(path.relative_to(self.svn_root))
            except ValueError as error:
                raise GitSvnError("Restore path must be inside the SVN working copy.") from error
        target = self.safe_path(value)
        value = str(target.relative_to(self.svn_root)).replace("\\", "/")
        entries = self.entries()
        current = [entry for entry in entries if self.safe_path(entry.path) == target]
        selected = self.file_snapshot(snapshot, value)
        if target.is_dir() or any(entry["kind"] == "dir" for entry in selected.entries):
            raise GitSvnError("Single-file restore requires a file; use revert for a whole branch.")
        for entry in entries:
            if self.safe_path(entry.path) != target and target.is_relative_to(self.safe_path(entry.path)):
                if entry.item not in ("none", "normal", "modified", "added") or entry.copied or entry.tree_conflicted:
                    raise GitSvnError(f"Cannot restore a file inside {entry.item} path: {entry.path}")
        self.protect_paths([(value, "file", "modified")], entries)
        if any(entry.item in ("unversioned", "ignored", "external") for entry in current):
            raise GitSvnError(f"Cannot restore an unversioned or excluded file: {value}")
        missing = any(entry.item == "missing" for entry in current)
        if missing or (not current and not selected.entries):
            info = ET.fromstring(self.svn("info", "--xml", "--depth", "empty", "--", self.target(value)))
            node = info.find("entry")
            if (node.get("kind") != "file" or
                    Path(node.findtext("wc-info/wcroot-abspath")).resolve() != self.svn_root):
                raise GitSvnError("Restore path must be a file in the configured SVN working copy.")
        if missing:
            if (node.findtext("wc-info/schedule") != "normal" or
                    any(entry.copied or entry.tree_conflicted or entry.props == "conflicted"
                        for entry in current)):
                raise GitSvnError("Cannot restore a missing added, copied, or conflicted file.")
            # No contents remain to save; retain pending properties and the missing state.
            properties = self.svn("diff", "--internal-diff", "--properties-only", "--depth",
                                  "empty", "--", self.target(value))
            backup = Snapshot("", properties, [], {})
        else:
            backup = self.capture(current)
        directory = self.state_dir / "restores"
        directory.mkdir(parents=True, exist_ok=True)
        recovery = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d_%H%M%S_%f_"), dir=directory))
        backup_path = recovery / "original.patch"
        selected_path = recovery / "restore.patch"
        self.write_restore_snapshot(backup_path, backup)
        self.write_restore_snapshot(selected_path, selected)
        save_json(recovery / "manifest.json", {"version": 1, "branch": self.branch,
                                               "head": head, "path": value, "missing": missing})
        reject = self.safe_path(value + ".svnpatch.rej")
        had_reject = reject.exists()
        try:
            self.revert_entries(current)
            self.apply_snapshot(selected_path, selected)
        except (GitSvnError, OSError, ValueError, ET.ParseError, KeyboardInterrupt) as error:
            try:
                self.revert_entries([entry for entry in self.entries() if self.safe_path(entry.path) == target])
                if not had_reject:
                    reject.unlink(missing_ok=True)
                self.apply_snapshot(backup_path, backup)
                if missing:
                    target.unlink(missing_ok=True)
            except (GitSvnError, OSError, ValueError, ET.ParseError, KeyboardInterrupt) as rollback_error:
                raise GitSvnError(f"File restore failed: {error}\nRollback also failed: {rollback_error}\n"
                                  f"Recovery copies: {recovery}") from rollback_error
            raise GitSvnError(f"File restore failed; previous changes restored. {error}\n"
                              f"Recovery copies: {recovery}") from error
        print(f"Restored {value} from {self.branch}/{head}. Recovery copies: {recovery}")
