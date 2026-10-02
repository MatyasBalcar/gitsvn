"""Local patch branches for an SVN working copy. No third-party dependencies."""

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


class GitSvnError(Exception):
    pass


@dataclass
class Entry:
    path: str
    item: str
    kind: str
    props: str = "none"
    ignored: bool = False
    copied: bool = False
    tree_conflicted: bool = False


@dataclass
class Snapshot:
    name: str
    patch: bytes
    entries: list
    empty_properties: dict


@contextmanager
def picker_input():
    if os.name != "nt" or not sys.stdin.isatty() or not sys.stdout.isatty():
        yield None
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_mode = kernel32.GetConsoleMode
    get_mode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_mode.restype = wintypes.BOOL
    set_mode = kernel32.SetConsoleMode
    set_mode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    set_mode.restype = wintypes.BOOL
    try:
        handle = msvcrt.get_osfhandle(sys.stdout.fileno())
    except (OSError, ValueError):
        yield None
        return
    mode = wintypes.DWORD()
    # Enable ANSI cursor movement in cmd and restore its original setting afterward.
    if not get_mode(handle, ctypes.byref(mode)) or not set_mode(handle, mode.value | 0x0004):
        yield None
        return
    try:
        yield msvcrt.getwch
    finally:
        set_mode(handle, mode.value)


def pick_item(labels, title, item, current=None, current_label="current"):
    labels = [" ".join(label.split()) for label in labels]
    with picker_input() as read_key:
        if read_key is None:
            print(title + (f" (* = {current_label}):" if current is not None else ":"))
            for index, label in enumerate(labels):
                print(f"{index + 1:3}. {'*' if index == current else ' '} {label}")
            try:
                choice = input(f"{item.capitalize()} number (q to cancel): ").strip()
            except EOFError:
                return None
            if choice.lower() in ("", "q", "quit"):
                return None
            if not choice.isdigit() or not 1 <= int(choice) <= len(labels):
                raise GitSvnError(f"Choose a {item} number shown in the list.")
            return int(choice) - 1

        selected = current if current is not None else 0
        size = shutil.get_terminal_size()
        rows = min(len(labels), max(1, size.lines - 3))
        width = max(1, size.columns - 1)
        print(f"{title} ({len(labels)} choices; Up/Down, Enter; Esc/q cancels)"[:width])
        try:
            sys.stdout.write("\x1b[?25l")
            while True:
                start = max(0, min(selected - rows // 2, len(labels) - rows))
                for index in range(start, start + rows):
                    line = "> " if index == selected else "  "
                    if index == current:
                        line += f"({current_label}) "
                    line += labels[index]
                    if index == selected:
                        line = "\x1b[7m" + line[:width] + "\x1b[0m"
                    else:
                        line = line[:width]
                    sys.stdout.write("\r\x1b[2K" + line + "\n")
                sys.stdout.flush()
                key = read_key()
                if key in ("\r", "\n"):
                    return selected
                if key in ("\x1b", "q", "Q"):
                    return None
                if key == "\x03":
                    raise KeyboardInterrupt
                if key in ("\x00", "\xe0"):
                    key = read_key()
                    if key == "H":
                        selected = (selected - 1) % len(labels)
                    elif key == "P":
                        selected = (selected + 1) % len(labels)
                    elif key == "G":
                        selected = 0
                    elif key == "O":
                        selected = len(labels) - 1
                sys.stdout.write(f"\x1b[{rows}A")
        finally:
            sys.stdout.write("\x1b[0m\x1b[?25h")
            sys.stdout.flush()


def pick_branch(names, current):
    selected = pick_item(names, "Switch branch", "branch", names.index(current))
    return names[selected] if selected is not None else None


class GitSvn:
    def __init__(self, svn_root=None, patch_root=None, state_dir=None, config_path=None):
        default_state_dir = Path(__file__).resolve().parent / ".gitsvn"
        if config_path is not None:
            self.config_path = Path(config_path).resolve()
            if not self.config_path.is_file():
                raise GitSvnError(f"Config file does not exist: {self.config_path}")
        else:
            config_dir = Path(state_dir).resolve() if state_dir is not None else default_state_dir
            self.config_path = config_dir / "config.json"
        self.config = {"autosave": True}
        if self.config_path.exists():
            try:
                config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
                if not isinstance(config, dict) or not isinstance(config.get("autosave", True), bool):
                    raise ValueError("autosave must be true or false")
                for key in ("svn_root", "patch_root", "state_dir"):
                    if key in config and (not isinstance(config[key], str) or not config[key].strip()):
                        raise ValueError(f"{key} must be a nonempty path string")
                self.config.update(config)
            except (ValueError, TypeError) as error:
                raise GitSvnError(f"Invalid config file: {self.config_path}: {error}") from error
        self.svn_root = self.configured_path(svn_root, "svn_root", Path(r"D:\Elektlabs"))
        self.patch_root = self.configured_path(patch_root, "patch_root", Path(r"D:\Patches"))
        self.state_dir = self.configured_path(state_dir, "state_dir", self.config_path.parent)
        if self.patch_root.is_relative_to(self.svn_root):
            raise GitSvnError("Patch storage must be outside the SVN working copy.")
        if self.state_dir.is_relative_to(self.svn_root):
            raise GitSvnError("State storage must be outside the SVN working copy.")
        self.state_path = self.state_dir / "state.json"
        self.state = {
            "version": 1,
            "svn_root": str(self.svn_root),
            "patch_root": str(self.patch_root),
            "branch": "trunk",
            "heads": {},
        }
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
                if self.state["version"] != 1:
                    raise ValueError("unsupported state version")
                for key, expected in (("svn_root", self.svn_root),
                                      ("patch_root", self.patch_root)):
                    if Path(self.state[key]).resolve() != expected:
                        raise GitSvnError(
                            "These paths belong to another workspace; use a separate --state-dir."
                        )
                self.branch_path(self.state["branch"])
                if not isinstance(self.state["heads"], dict):
                    raise ValueError("invalid branch heads")
            except (ValueError, KeyError, TypeError) as error:
                raise GitSvnError(f"Invalid state file: {self.state_path}: {error}") from error

    def configured_path(self, value, key, default):
        if value is not None:
            return Path(value).resolve()
        if key not in self.config:
            return Path(default).resolve()
        path = Path(self.config[key])
        if not path.is_absolute():
            path = self.config_path.parent / path
        return path.resolve()

    @property
    def branch(self):
        return self.state["branch"]

    def save_state(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.state_dir,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(self.state, handle, indent=2)
            handle.write("\n")
        try:
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def svn(self, *arguments):
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        try:
            result = subprocess.run(["svn", *arguments], cwd=self.svn_root,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=environment, check=False)
        except FileNotFoundError as error:
            raise GitSvnError("SVN or the working-copy directory was not found.") from error
        if result.returncode:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitSvnError(f"svn {arguments[0]} failed: {message}")
        return result.stdout

    def verify_working_copy(self):
        actual = self.svn("info", "--show-item", "wc-root", "--", ".")
        if Path(actual.decode("utf-8").strip()).resolve() != self.svn_root:
            raise GitSvnError("--svn-root must point to the SVN working-copy root.")

    def branch_path(self, name):
        if (not isinstance(name, str) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or
                name.endswith(".") or
                re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name)):
            raise GitSvnError("Invalid branch name; use letters, numbers, dots, '-' or '_'.")
        path = (self.patch_root / name).resolve()
        if path.parent != self.patch_root:
            raise GitSvnError("Branch folder must stay inside patch storage.")
        if self.state_dir.is_relative_to(path) or Path(__file__).resolve().is_relative_to(path):
            raise GitSvnError("That folder is reserved for gitsvn itself.")
        return path

    def safe_path(self, value):
        if not isinstance(value, str):
            raise GitSvnError("Working-copy paths must be strings.")
        value = value.replace("\\", "/")
        if (not value or value.startswith("/") or ":" in value or
                any(part == ".." or part.casefold() == ".svn" or
                    (part not in ("", ".") and part.endswith((".", " ")))
                    for part in value.split("/"))):
            raise GitSvnError(f"Unsafe working-copy path: {value}")
        path = self.svn_root / value
        current = path
        while current != self.svn_root:
            if current.is_symlink() or current.is_junction():
                raise GitSvnError(f"Links cannot be restored safely: {value}")
            current = current.parent
        if not path.resolve().is_relative_to(self.svn_root):
            raise GitSvnError(f"Path leaves the SVN working copy: {value}")
        return path

    @staticmethod
    def target(path):
        return path + "@" if "@" in path else path

    def entries(self):
        xml = ET.fromstring(self.svn("status", "--xml", "--no-ignore", "--ignore-externals", "."))
        entries = []
        for parent in xml:
            ignored = (parent.tag == "changelist" and
                       parent.get("name", "").lower() == "ignore-on-commit")
            for node in parent.findall("entry"):
                status = node.find("wc-status")
                if status is None:
                    continue
                path = node.get("path", "").replace("\\", "/")
                full_path = self.safe_path(path)
                kind = "dir" if full_path.is_dir() else "file"
                item = status.get("item", "none")
                if item == "deleted" and not full_path.exists():
                    info = ET.fromstring(self.svn("info", "--xml", "--depth", "empty",
                                                 "--", self.target(path)))
                    kind = info.find("entry").get("kind")
                entries.append(Entry(path, item, kind, status.get("props", "none"),
                                     ignored, status.get("copied") == "true",
                                     status.get("tree-conflicted") == "true"))
        return entries

    @staticmethod
    def changes(entries):
        return [entry for entry in entries if not entry.ignored and
                (entry.item not in ("none", "normal", "unversioned", "ignored", "external") or
                 entry.props in ("modified", "conflicted") or entry.tree_conflicted)]

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

    def protect_paths(self, paths, entries):
        for value, kind, item in paths:
            path = self.safe_path(value)
            for entry in entries:
                protected = self.safe_path(entry.path)
                if entry.ignored and (path == protected or path.is_relative_to(protected) or
                                      (kind == "dir" and item in ("deleted", "added") and
                                       protected.is_relative_to(path))):
                    raise GitSvnError(f"Operation would affect ignore-on-commit: {entry.path}")
                if entry.item in ("unversioned", "ignored"):
                    if (path.exists() and path.is_relative_to(protected) and
                            not (kind == "dir" and item == "added" and path.is_dir())):
                        raise GitSvnError(f"Operation would overwrite unversioned data: {entry.path}")
                    if kind == "dir" and item == "deleted" and protected.is_relative_to(path):
                        raise GitSvnError(f"Operation would delete unversioned data: {entry.path}")

    def revert_entries(self, entries):
        for entry in sorted(self.changes(entries),
                            key=lambda value: (value.path.count("/"), value.path), reverse=True):
            depth = "infinity" if entry.item == "deleted" and entry.kind == "dir" else "empty"
            arguments = ["revert", "--depth", depth]
            if entry.item == "added" and entry.kind == "file":
                arguments.append("--remove-added")
            self.svn(*arguments, "--", self.target(entry.path))
            if entry.item == "added" and entry.kind == "dir":
                try:
                    self.safe_path(entry.path).rmdir()
                except OSError:
                    pass  # Keep directories containing the user's unversioned files.

    @staticmethod
    def check_patch_output(output):
        text = output.decode("utf-8", errors="replace")
        if any(re.match(r"^(?:[A-Z !]?C|![ A-Z]?|Skipped\b|>.*rejected\b)", line)
               for line in text.splitlines()):
            raise GitSvnError(f"Patch could not be applied cleanly:\n{text.strip()}")
        if text.strip():
            print(text.rstrip())

    def apply_patch(self, path):
        if path.stat().st_size:
            self.check_patch_output(self.svn("patch", "--dry-run", str(path), "."))
            self.check_patch_output(self.svn("patch", str(path), "."))

    def apply_snapshot(self, path, snapshot):
        additions = sorted((entry for entry in snapshot.entries if entry["item"] == "added"),
                           key=lambda entry: (entry["path"].count("/"), entry["path"]))
        for entry in additions:
            target = self.safe_path(entry["path"])
            if entry["kind"] == "dir":
                target.mkdir(parents=True, exist_ok=True)
                self.svn("add", "--depth", "empty", "--", self.target(entry["path"]))
            elif entry.get("empty"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"")
                self.svn("add", "--depth", "empty", "--", self.target(entry["path"]))
        self.apply_patch(path)
        current = {entry.path: entry for entry in self.entries()}
        for entry in snapshot.entries:
            target = self.safe_path(entry["path"])
            actual = current.get(entry["path"])
            if entry["item"] == "modified" and entry.get("empty"):
                if actual and actual.item == "deleted":
                    self.svn("revert", "--depth", "empty", "--", self.target(entry["path"]))
                target.write_bytes(b"")
                properties = snapshot.empty_properties.get(entry["path"])
                if properties and actual and actual.item == "deleted":
                    with tempfile.TemporaryDirectory(dir=self.state_dir) as temporary:
                        property_patch = Path(temporary) / "properties.patch"
                        property_patch.write_bytes(base64.b64decode(properties, validate=True))
                        self.apply_patch(property_patch)
        # SVN text patches omit empty deletions and directory deletions.
        for entry in sorted(snapshot.entries, key=lambda item: item["path"].count("/"),
                            reverse=True):
            if entry["item"] == "deleted":
                actual = current.get(entry["path"])
                if not actual or actual.item != "deleted":
                    self.svn("delete", "--", self.target(entry["path"]))

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
        output = self.svn("update", "--ignore-externals", ".")
        print(output.decode("utf-8", errors="replace").rstrip())
        if any(entry.item == "conflicted" or entry.props == "conflicted" or entry.tree_conflicted
               for entry in self.entries()):
            raise GitSvnError("SVN update produced conflicts. Resolve them before switching branches.")


def parser():
    result = argparse.ArgumentParser(prog="gitsvn", description="Local Git-like patch branches for SVN.")
    result.add_argument("--config", help="Settings file; defaults to .gitsvn/config.json beside the program")
    result.add_argument("--svn-root", help="SVN working-copy root; overrides config")
    result.add_argument("--patch-root", help="Patch storage folder; overrides config")
    result.add_argument("--state-dir", help="State folder; also selects config unless --config is supplied")
    commands = result.add_subparsers(dest="command", required=True)
    branch = commands.add_parser("branch", help="List branches or initialize a clean-trunk branch")
    branch.add_argument("name", nargs="?")
    switch = commands.add_parser("switch", help="Restore a branch using the autosave setting")
    switch.add_argument("name", nargs="?", help="Branch to restore; omit to open the branch picker")
    commit = commands.add_parser("commit", help="Save a timestamped patch and message")
    commit.add_argument("-m", "--message", required=True)
    finalize = commands.add_parser("finalize", help="Export a date/description-named ticket patch")
    finalize.add_argument("description")
    finalize.add_argument("ticket", nargs="?", help="Destination ticket; defaults to current branch")
    finalize.add_argument("--latest", action="store_true", help="Export the branch's saved snapshot")
    log = commands.add_parser("log", help="List snapshot timestamps and messages")
    log.add_argument("branch", nargs="?")
    revert = commands.add_parser("revert", help="Choose a saved snapshot to restore")
    revert.add_argument("branch", nargs="?")
    revert.add_argument("snapshot", nargs="?", help="Snapshot ID; omit to open the snapshot picker")
    commands.add_parser("pull", help="Autosave changes and run svn update")
    commands.add_parser("status", help="Show active branch and SVN status")
    add = commands.add_parser("add", help="Schedule working-copy paths with svn add")
    add.add_argument("paths", nargs="+")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        app = GitSvn(arguments.svn_root, arguments.patch_root, arguments.state_dir, arguments.config)
        if (arguments.command in ("switch", "commit", "revert", "pull", "status", "add") or
                (arguments.command == "finalize" and not arguments.latest)):
            app.verify_working_copy()
        if arguments.command == "branch":
            app.create_branch(arguments.name) if arguments.name else app.list_branches()
        elif arguments.command == "switch":
            app.switch(arguments.name)
        elif arguments.command == "commit":
            app.commit(arguments.message)
        elif arguments.command == "finalize":
            app.finalize(arguments.description, arguments.ticket, arguments.latest)
        elif arguments.command == "log":
            app.list_history(arguments.branch or app.branch)
        elif arguments.command == "revert":
            app.select_restore(arguments.branch or app.branch, arguments.snapshot)
        elif arguments.command == "pull":
            app.pull()
        elif arguments.command == "status":
            print(f"On branch {app.branch}")
            print(app.svn("status", "--ignore-externals", ".").decode("utf-8", errors="replace"),
                  end="")
        elif arguments.command == "add":
            for path in arguments.paths:
                full_path = Path(path)
                value = str(full_path.relative_to(app.svn_root)) if full_path.is_absolute() else path
                app.safe_path(value)
                print(app.svn("add", "--", app.target(value)).decode("utf-8", errors="replace"),
                      end="")
        return 0
    except (GitSvnError, OSError, ValueError, ET.ParseError) as error:
        print(f"gitsvn: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("gitsvn: Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
