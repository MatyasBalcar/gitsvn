"""SVN access, working-copy paths, and safe snapshot application."""

import base64
import os
from pathlib import Path
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from .models import Entry, GitSvnError


def svn_output(directory, *arguments):
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    try:
        result = subprocess.run(["svn", *arguments], cwd=directory,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=environment, check=False)
    except FileNotFoundError as error:
        raise GitSvnError("SVN or the working-copy directory was not found.") from error
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitSvnError(f"svn {arguments[0]} failed: {message}")
    return result.stdout


def working_copy_root(directory):
    output = svn_output(directory, "info", "--show-item", "wc-root", "--", ".")
    return Path(output.decode("utf-8").strip()).resolve()


def detect_working_copy_root(directory):
    for parent in (directory, *directory.parents):
        if (parent / ".svn").is_dir():
            return working_copy_root(parent)
    raise GitSvnError("Current directory is not inside an SVN working copy.")


class WorkingCopyMixin:
    def svn(self, *arguments):
        return svn_output(self.svn_root, *arguments)

    def verify_working_copy(self):
        if working_copy_root(self.svn_root) != self.svn_root:
            raise GitSvnError("--svn-root must point to the SVN working-copy root.")

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
