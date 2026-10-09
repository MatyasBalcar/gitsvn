"""Local branch commands, restore recovery, and finalized patch export."""

from datetime import datetime
import json
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from .models import GitSvnError, Snapshot
from .terminal import pick_branch, pick_item


class BranchMixin:
    def create_branch(self, name):
        directory = self.branch_path(name)
        if directory.is_dir() and self.history(name):
            print(f"Branch {name} already exists.")
            return
        self.save_snapshot(name, Snapshot("", b"", [], {}), f"Created branch {name} from trunk")
        print(f"Use gitsvn switch {name} to select it.")

    def branches(self):
        names = {self.branch}
        if self.patch_root.is_dir():
            for directory in self.patch_root.iterdir():
                if directory.is_dir() and any(directory.glob("*.patch")):
                    try:
                        self.branch_path(directory.name)
                    except GitSvnError:
                        continue
                    names.add(directory.name)
        return sorted(names)

    def list_branches(self):
        for name in self.branches():
            print(f"{'*' if name == self.branch else ' '} {name}")

    def restore(self, branch, path, autosave=True):
        snapshot = self.load_snapshot(path)
        entries = self.entries()
        details = {entry["path"]: (entry["kind"], entry["item"]) for entry in snapshot.entries}
        touched = []
        for value in self.patch_paths(snapshot):
            kind, item = details.get(value, ("dir" if self.safe_path(value).is_dir() else "file",
                                             "modified"))
            touched.append((value, kind, item))
        self.protect_paths(touched, entries)
        rejects = {self.safe_path(value + ".svnpatch.rej") for value in self.patch_paths(snapshot)}
        existing_rejects = {value for value in rejects if value.exists()}
        backup = self.capture(entries)
        old_branch = self.branch
        head = self.state["heads"].get(old_branch)
        head_path = self.branch_path(old_branch) / (head + ".patch") if head else None
        was_dirty = autosave and head_path and head_path.exists() and (
            head_path.stat().st_size or self.load_snapshot(head_path).entries)
        temporary_backup = None
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if autosave and (backup.entries or was_dirty):
            self.save_snapshot(old_branch, backup, f"Autosave before restoring {branch}/{path.stem}")
            backup_path = self.branch_path(old_branch) / (backup.name + ".patch")
        elif backup.entries:
            try:
                with tempfile.NamedTemporaryFile(prefix="switch-", suffix=".patch",
                                                 dir=self.state_dir, delete=False) as handle:
                    temporary_backup = Path(handle.name)
                    handle.write(backup.patch)
                temporary_backup.with_suffix(".json").write_text(json.dumps(
                    {"version": 1, "entries": backup.entries,
                     "empty_properties": backup.empty_properties}, indent=2), encoding="utf-8")
            except OSError:
                if temporary_backup:
                    temporary_backup.unlink(missing_ok=True)
                    temporary_backup.with_suffix(".json").unlink(missing_ok=True)
                raise
            backup_path = temporary_backup
        else:
            backup_path = None
        previous_state = self.state
        preserve_backup = True
        try:
            self.revert_entries(entries)
            self.apply_snapshot(path, snapshot)
            self.state = {**previous_state, "branch": branch,
                          "heads": {**previous_state["heads"], branch: path.stem}}
            self.save_state()
            preserve_backup = False
        except (GitSvnError, OSError, ValueError, ET.ParseError, KeyboardInterrupt) as error:
            self.state = previous_state
            try:
                self.revert_entries(self.entries())
                for reject in rejects - existing_rejects:
                    reject.unlink(missing_ok=True)
                if backup_path:
                    self.apply_snapshot(backup_path, backup)
            except (GitSvnError, OSError, ValueError, ET.ParseError, KeyboardInterrupt) as rollback_error:
                raise GitSvnError(
                    f"Restore failed: {error}\nRollback also failed: {rollback_error}\n"
                    f"Recovery snapshot: {backup_path or '(working copy was clean)'}"
                ) from rollback_error
            preserve_backup = False
            raise GitSvnError(f"Restore failed; previous changes restored. {error}") from error
        finally:
            if temporary_backup and not preserve_backup:
                temporary_backup.unlink(missing_ok=True)
                temporary_backup.with_suffix(".json").unlink(missing_ok=True)
        print(f"On branch {branch}, restored {path.stem}.")

    def switch(self, branch=None):
        if branch is None:
            branch = pick_branch(sorted(set(self.branches()) | {"trunk"}), self.branch)
            if branch is None:
                print("Switch cancelled.")
                return
        if branch == self.branch:
            print(f"Already on branch {branch}.")
            return
        if branch == "trunk" and not self.branch_path(branch).is_dir():
            self.create_branch(branch)
        history = self.history(branch)
        if not history:
            raise GitSvnError(f"No snapshots in {branch}; run gitsvn branch {branch} first.")
        head = self.state["heads"].get(branch)
        path = next((path for path in history if path.stem == head), history[0])
        self.restore(branch, path, autosave=self.config["autosave"])

    def select_restore(self, branch, name):
        if name:
            path = next((path for path in self.history(branch)
                         if path.stem == name or path.name == name), None)
            if path is None:
                raise GitSvnError(f"Snapshot does not exist: {branch}/{name}")
        else:
            history = self.history(branch)
            if not history:
                print(f"No snapshots in {branch}.")
                return
            head = self.state["heads"].get(branch)
            current = next((index for index, path in enumerate(history) if path.stem == head), None)
            choice = pick_item([self.snapshot_label(path) for path in history],
                               f"Revert snapshot in {branch}", "snapshot", current, "saved head")
            if choice is None:
                print("Restore cancelled.")
                return
            path = history[choice]
        self.restore(branch, path)

    def commit(self, message):
        if not message.strip():
            raise GitSvnError("Commit message cannot be empty.")
        snapshot = self.capture(self.entries())
        self.save_snapshot(self.branch, snapshot, message)

    def finalize(self, description, ticket, latest=False):
        name = re.sub(r"\s+", "_", description.strip())
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise GitSvnError(
                "Description must start with a letter or number and contain only "
                "letters, numbers, spaces, dots, '-' or '_'."
            )
        branch = ticket or self.branch
        self.branch_path(branch)
        name = datetime.now().strftime("%Y_%m_%d_") + name + "_BalcarM.patch"
        patch_path = self.patch_root / name
        if patch_path.exists() or patch_path.is_symlink():
            raise GitSvnError(f"Finalized patch already exists: {patch_path}. Choose another description.")
        if latest:
            history = self.history(self.branch)
            if not history:
                raise GitSvnError(f"No snapshots in {self.branch}; commit first or omit --latest.")
            head = self.state["heads"].get(self.branch)
            path = next((path for path in history if path.stem == head), history[0])
            snapshot = self.load_snapshot(path)
        else:
            snapshot = self.capture(self.entries())
        self.patch_root.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with patch_path.open("xb") as handle:
                created = True
                handle.write(snapshot.patch)
            self.save_snapshot(branch, snapshot, description)
        except (GitSvnError, OSError, KeyboardInterrupt):
            if created:
                patch_path.unlink(missing_ok=True)
            raise
        print(f"Exported {patch_path}")

    def pull(self):
        entries = self.entries()
        snapshot = self.capture(entries)
        if snapshot.entries:
            self.save_snapshot(self.branch, snapshot, "Autosave before svn update")
        output = self.svn("update", "--accept", "postpone", "--ignore-externals", ".")
        print(output.decode("utf-8", errors="replace").rstrip())
        if any(entry.item == "conflicted" or entry.props == "conflicted" or entry.tree_conflicted
               for entry in self.entries()):
            raise GitSvnError("SVN update produced conflicts. Run gitsvn resolve before switching branches.")
