"""Snapshot capture, storage, history, and SVN patch parsing."""

import base64
from datetime import datetime
import json
import re

from .models import GitSvnError, Snapshot


class SnapshotMixin:
    @staticmethod
    def patch_sections(patch):
        sections = {}
        path = None
        item = "modified"
        lines = []
        old_lines = new_lines = 0

        def save_section():
            if path is not None:
                # SVN emits only an Index header for an empty file addition.
                change = (item if lines or item != "modified" else "added", tuple(lines))
                if path in sections and sections[path] != change:
                    raise GitSvnError(f"Conflicting patch sections for {path}.")
                sections[path] = change

        for raw_line in patch.removeprefix(b"\xef\xbb\xbf").splitlines(keepends=True):
            line = raw_line.rstrip(b"\r\n")
            if old_lines or new_lines:
                # Keep payload bytes, including line endings and header-like text.
                lines.append(raw_line)
                if line.startswith(b"-"):
                    old_lines -= 1
                elif line.startswith(b"+"):
                    new_lines -= 1
                elif line.startswith(b" "):
                    old_lines -= 1
                    new_lines -= 1
                continue
            value = None
            if line.startswith(b"Index: "):
                value = line[7:]
            elif line.startswith(b"Property changes on: "):
                value = line[21:]
            elif line.startswith((b"--- ", b"+++ ")):
                value = line[4:].split(b"\t", 1)[0]
            if value is not None and value != b"/dev/null":
                value = value.decode("utf-8-sig", errors="replace").replace("\\", "/")
                if line.startswith(b"Index: ") or (path is not None and value != path):
                    save_section()
                    item = "modified"
                    lines = []
                path = value
            if line.startswith((b"--- ", b"+++ ")):
                if value == b"/dev/null" or line.endswith((b"\t(nonexistent)", b"\t(revision 0)")):
                    item = "added" if line.startswith(b"--- ") else "deleted"
                # Revision labels and working-copy labels are display details.
                continue
            if line.startswith(b"Property changes on: "):
                lines.append(b"Property changes on:")
                continue
            if line.startswith(b"Index: ") or line in (b"", b"=" * 67, b"_" * 67):
                continue
            hunk = re.match(br"^(?:@@|##) -\d+(?:,(\d+))? \+\d+(?:,(\d+))? (?:@@|##)", line)
            if hunk:
                old_lines = int(hunk[1]) if hunk[1] is not None else 1
                new_lines = int(hunk[2]) if hunk[2] is not None else 1
            lines.append(line)
        save_section()
        return sections

    def capture(self, entries):
        changes = self.changes(entries)
        patch = bytearray()
        metadata = []
        empty_properties = {}
        deleted_directories = [entry.path for entry in changes
                               if entry.item == "deleted" and entry.kind == "dir"]
        for entry in sorted(changes, key=lambda item: item.path):
            if (entry.item not in ("normal", "modified", "added", "deleted") or
                    entry.props == "conflicted" or entry.copied or entry.tree_conflicted):
                raise GitSvnError(
                    f"Cannot snapshot {entry.path}: {entry.item}, copied={entry.copied}. "
                    "Resolve conflicts, missing files and SVN copies first."
                )
            self.protect_paths([(entry.path, entry.kind, entry.item)], entries)
            path = self.safe_path(entry.path)
            if any(entry.path != parent and path.is_relative_to(self.svn_root / parent)
                   for parent in deleted_directories):
                continue
            if entry.item == "deleted" and path.exists():
                raise GitSvnError(
                    f"Deleted path still contains local data: {entry.path}. Move it aside first."
                )
            depth = "infinity" if entry.path in deleted_directories else "empty"
            diff = self.svn("diff", "--internal-diff", "--depth", depth, "--",
                            self.target(entry.path))
            if self.is_binary_patch(diff):
                raise GitSvnError(f"Binary changes cannot be stored in a text patch: {entry.path}")
            patch.extend(diff)
            empty = entry.kind == "file" and path.exists() and path.stat().st_size == 0
            metadata.append({"path": entry.path, "item": entry.item,
                             "kind": entry.kind, "empty": empty})
            if empty and entry.item == "modified" and entry.props == "modified":
                props = self.svn("diff", "--internal-diff", "--properties-only", "--depth",
                                 "empty", "--", self.target(entry.path))
                empty_properties[entry.path] = base64.b64encode(props).decode("ascii")
        return Snapshot("", bytes(patch), metadata, empty_properties)

    def save_snapshot(self, branch, snapshot, message):
        directory = self.branch_path(branch)
        name = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        for extension in (".patch", ".json", ".txt"):
            path = directory / (name + extension)
            if path.exists() or path.is_symlink():
                raise GitSvnError(f"Snapshot already exists: {path}. Choose a different name.")
        directory.mkdir(parents=True, exist_ok=True)
        created = []
        try:
            for extension, contents in (
                    (".patch", snapshot.patch),
                    (".json", json.dumps({"version": 1, "entries": snapshot.entries,
                                          "empty_properties": snapshot.empty_properties},
                                         indent=2).encode("utf-8")),
                    (".txt", (message.strip() + "\n").encode("utf-8"))):
                path = directory / (name + extension)
                with path.open("xb") as handle:
                    created.append(path)
                    handle.write(contents)
        except OSError:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        snapshot.name = name
        self.state["heads"][branch] = name
        self.save_state()
        print(f"Saved {branch}/{name}: {message.strip()}")
        return name

    def history(self, branch):
        directory = self.branch_path(branch)
        if not directory.is_dir():
            raise GitSvnError(f"Branch does not exist: {branch}. Use gitsvn branch {branch}.")
        paths = [path for path in directory.glob("*.patch") if path.is_file()]
        return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)

    @staticmethod
    def message(path):
        message = path.with_suffix(".txt")
        return (message.read_text(encoding="utf-8-sig", errors="replace").strip()
                if message.is_file() else "(no message)")

    def snapshot_label(self, path):
        try:
            saved_at = datetime.strptime(path.stem, "%Y%m%d_%H%M%S_%f")
        except ValueError:
            saved_at = datetime.fromtimestamp(path.stat().st_mtime)
        return f"{saved_at:%d.%m.%Y %H:%M:%S}  {self.message(path)}  [{path.stem}]"

    def list_history(self, branch):
        paths = self.history(branch)
        head = self.state["heads"].get(branch)
        for number, path in enumerate(paths, 1):
            marker = " (current version)" if path.stem == head else ""
            print(f"{number:>3}. {self.snapshot_label(path)}{marker}")
        if not paths:
            print(f"No snapshots in {branch}.")
        return paths

    def load_snapshot(self, path):
        patch = path.read_bytes()
        if self.is_binary_patch(patch):
            raise GitSvnError("This patch contains binary changes that SVN cannot restore.")
        metadata_path = path.with_suffix(".json")
        entries = []
        empty_properties = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata["version"] != 1:
                    raise ValueError("unsupported snapshot version")
                entries = metadata["entries"]
                empty_properties = metadata.get("empty_properties", {})
                if not isinstance(entries, list) or not isinstance(empty_properties, dict):
                    raise ValueError("invalid entries or empty-file properties")
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError("invalid snapshot entry")
                    self.safe_path(entry["path"])
                    if (entry["kind"] not in ("file", "dir") or
                            entry["item"] not in ("normal", "modified", "added", "deleted") or
                            not isinstance(entry.get("empty", False), bool)):
                        raise ValueError("unsupported entry")
                for value, properties in empty_properties.items():
                    if not isinstance(properties, str) or not any(
                            entry["path"] == value and entry.get("empty") and
                            entry["item"] == "modified" for entry in entries):
                        raise ValueError("invalid empty-file property patch")
                    embedded = Snapshot("", base64.b64decode(properties, validate=True), [], {})
                    if self.patch_paths(embedded) - {value}:
                        raise ValueError("empty-file property patch affects another path")
            except (ValueError, KeyError, TypeError) as error:
                raise GitSvnError(f"Invalid snapshot metadata: {error}") from error
        snapshot = Snapshot(path.stem, patch, entries, empty_properties)
        self.patch_paths(snapshot)
        return snapshot

    @staticmethod
    def is_binary_patch(patch):
        return any(line.startswith((b"Cannot display:", b"Binary files "))
                   for line in patch.splitlines())

    def patch_paths(self, snapshot):
        paths = {entry["path"] for entry in snapshot.entries}
        old_lines = new_lines = 0
        for line in snapshot.patch.decode("utf-8-sig", errors="replace").splitlines():
            if old_lines or new_lines:
                if line.startswith("-"):
                    if not old_lines:
                        raise GitSvnError("Malformed patch hunk.")
                    old_lines -= 1
                elif line.startswith("+"):
                    if not new_lines:
                        raise GitSvnError("Malformed patch hunk.")
                    new_lines -= 1
                elif line.startswith(" "):
                    if not old_lines or not new_lines:
                        raise GitSvnError("Malformed patch hunk.")
                    old_lines -= 1
                    new_lines -= 1
                elif not line.startswith("\\"):
                    raise GitSvnError("Malformed patch hunk.")
                continue
            hunk = re.match(r"^(?:@@|##) -\d+(?:,(\d+))? \+\d+(?:,(\d+))? (?:@@|##)", line)
            if hunk:
                old_lines = int(hunk[1]) if hunk[1] is not None else 1
                new_lines = int(hunk[2]) if hunk[2] is not None else 1
                continue
            if line.startswith("Index: "):
                paths.add(line[7:])
            elif line.startswith("Property changes on: "):
                paths.add(line[21:])
            elif line.startswith(("--- ", "+++ ")):
                value = line[4:].split("\t", 1)[0]
                if value != "/dev/null":
                    paths.add(value)
        if old_lines or new_lines:
            raise GitSvnError("Incomplete patch hunk.")
        for path in paths:
            self.safe_path(path)
        if snapshot.patch.strip() and not paths:
            raise GitSvnError("This is not a supported SVN unified patch.")
        return paths
