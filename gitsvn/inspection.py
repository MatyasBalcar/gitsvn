"""Saved-head comparison, branch status, and snapshot inspection."""

import base64

from .comparison import saved_changes
from .conflicts import property_values
from .conflict_ui import file_diff
from .models import GitSvnError
from .terminal import ansi_output, pick_item, terminal_text, view_diff


class InspectionMixin:
    def snapshot_sections(self, snapshot):
        sections = self.patch_sections(snapshot.patch)
        for value, properties in snapshot.empty_properties.items():
            value = value.replace("\\", "/")
            embedded = self.patch_sections(base64.b64decode(properties, validate=True)).get(value)
            if embedded is not None:
                item, lines = sections.get(value, ("modified", ()))
                if b"Property changes on:" in lines:
                    lines = lines[:lines.index(b"Property changes on:")]
                sections[value] = (item, lines + embedded[1])
        return sections

    def unsaved_changes(self, changes):
        if self.branch not in self.state["heads"]:
            return {entry.path for entry in changes}
        head = self.state["heads"][self.branch]
        try:
            path = next((path for path in self.history(self.branch) if path.stem == head), None)
            if path is None:
                raise GitSvnError(f"Snapshot does not exist: {self.branch}/{head}")
            snapshot = self.load_snapshot(path)
            sections = self.snapshot_sections(snapshot)
        except (GitSvnError, OSError, ValueError) as error:
            raise GitSvnError(f"Cannot compare status with current head: {error}") from error
        metadata = {entry["path"].replace("\\", "/"): entry for entry in snapshot.entries}
        for value, details in metadata.items():
            if value in sections:
                item = "modified" if details["item"] == "normal" else details["item"]
                sections[value] = (item, sections[value][1])
        deleted_directories = [value for value, entry in metadata.items()
                               if entry["item"] == "deleted" and entry["kind"] == "dir"]
        unsaved = set()
        for entry in changes:
            if (entry.item not in ("normal", "modified", "added", "deleted") or
                    entry.props == "conflicted" or entry.copied or entry.tree_conflicted):
                unsaved.add(entry.path)
                continue
            full_path = self.safe_path(entry.path)
            if entry.item == "deleted" and full_path.exists():
                unsaved.add(entry.path)
                continue
            recursive = entry.kind == "dir" and entry.item in ("added", "deleted")
            saved = {value: section for value, section in sections.items()
                     if value == entry.path or (recursive and value.startswith(entry.path + "/"))}
            details = metadata.get(entry.path)
            item = "modified" if entry.item == "normal" else entry.item
            if details is not None:
                saved_item = "modified" if details["item"] == "normal" else details["item"]
                empty = entry.kind == "file" and full_path.exists() and full_path.stat().st_size == 0
                if (saved_item != item or details["kind"] != entry.kind or
                        ("empty" in details and details["empty"] != empty)):
                    unsaved.add(entry.path)
                    continue
            elif entry.path in sections:
                if sections[entry.path][0] != item:
                    unsaved.add(entry.path)
                    continue
            elif not (item == "deleted" and any(entry.path.startswith(parent + "/")
                                                for parent in deleted_directories)):
                # Legacy patches can imply a parent operation through their children.
                if not (not metadata and recursive and saved and
                        all(section[0] == item for section in saved.values())):
                    unsaved.add(entry.path)
                    continue
            depth = "infinity" if recursive else "empty"
            diff = self.svn("diff", "--internal-diff", "--depth", depth, "--", self.target(entry.path))
            if self.is_binary_patch(diff):
                unsaved.add(entry.path)
                continue
            current = self.patch_sections(diff)
            if entry.path in current:
                current[entry.path] = (item, current[entry.path][1])
            if current != saved:
                unsaved.add(entry.path)
        return unsaved

    def status(self):
        changes = sorted(self.changes(self.entries()), key=lambda entry: entry.path)
        unsaved = self.unsaved_changes(changes)
        print(f"On branch {self.branch}")
        if not changes:
            print("No tracked branch changes.")
            return
        symbols = {"added": "A", "modified": "M", "normal": "M", "deleted": "D",
                   "replaced": "R", "missing": "!", "incomplete": "!",
                   "conflicted": "C", "obstructed": "~"}
        with ansi_output() as enabled:
            for entry in changes:
                symbol = ("C" if entry.props == "conflicted" or entry.tree_conflicted
                          else symbols.get(entry.item, "?"))
                line = f"{symbol}  {entry.path}"
                if enabled:
                    color = 32 if symbol == "A" else 31 if symbol in ("D", "C", "!", "~") else 33
                    filename_color = 31 if entry.path in unsaved else 37
                    line = (f"\x1b[{color}m{symbol}\x1b[0m  "
                            f"\x1b[{filename_color}m{entry.path}\x1b[0m")
                print(line)

    def inspect_changes(self, snapshot):
        sections = self.snapshot_sections(snapshot)
        metadata = {entry["path"]: entry for entry in snapshot.entries}
        entries = self.entries()
        changes = {entry.path: entry for entry in self.changes(entries)}
        excluded = [self.safe_path(entry.path) for entry in entries if entry.ignored or
                    entry.item in ("unversioned", "ignored", "external")]
        deleted = [self.safe_path(entry["path"]) for entry in snapshot.entries
                   if entry["kind"] == "dir" and entry["item"] == "deleted"]
        differences = {}
        for value in sorted(self.patch_paths(snapshot) | changes.keys()):
            path = self.safe_path(value)
            if any(path == other or path.is_relative_to(other) for other in excluded):
                continue
            entry = changes.get(value)
            details = metadata.get(value, {})
            kind = details.get("kind", entry.kind if entry else "dir" if path.is_dir() else "file")
            item, lines = sections.get(value, ("modified", ()))
            item = details.get("item", item)
            head_absent = item == "deleted" or any(path.is_relative_to(parent) for parent in deleted)
            base_absent = item == "added" or (entry and entry.item == "added" and
                                               value not in sections and value not in metadata)
            base = properties = None
            if head_absent:
                before, before_props = None, {}
            else:
                base = b"" if base_absent or kind == "dir" else self.svn(
                    "cat", "--ignore-keywords", "-r", "BASE", "--", self.target(value))
                properties = {} if base_absent else property_values(self.svn(
                    "proplist", "--xml", "--verbose", "--depth", "empty", "-r", "BASE", "--", self.target(value)))
                before, before_props = saved_changes(base, properties, lines)
                if base_absent and item != "added":
                    before, before_props = None, {}
            after = None
            after_props = {}
            if path.exists() and not (entry and entry.item in ("deleted", "missing")):
                after = path.read_bytes() if path.is_file() else b""
                after_props = property_values(self.svn("proplist", "--xml", "--verbose", "--depth",
                                                       "empty", "--", self.target(value)))
                if kind == "file" and any(values.get("svn:keywords") for values in
                                          (before_props, after_props, properties or {})):
                    if base is None:
                        base = self.svn("cat", "--ignore-keywords", "-r", "BASE", "--", self.target(value))
                        properties = property_values(self.svn(
                            "proplist", "--xml", "--verbose", "--depth", "empty", "-r", "BASE", "--", self.target(value)))
                    # SVN contracts standard, fixed-width, and custom keywords for its diff.
                    patch = self.svn("diff", "--internal-diff", "--depth", "empty", "--", self.target(value))
                    if not self.is_binary_patch(patch):
                        current = self.patch_sections(patch).get(value, ("modified", ()))
                        after, _ = saved_changes(base, properties, current[1])
            if before_props.get("svn:eol-style") or after_props.get("svn:eol-style"):
                if before is not None:
                    before = before.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                if after is not None:
                    after = after.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            if (before, before_props) == (after, after_props):
                continue
            action = "added" if before is None else "deleted" if after is None else "modified"
            display = [f"{action.capitalize()} {kind}: {value}", ""]
            if kind == "file":
                display.extend(file_diff(before or b"", after or b"", value, "saved head", "working copy"))
            for name in sorted(before_props.keys() | after_props.keys()):
                if before_props.get(name) != after_props.get(name):
                    operation = "Added" if name not in before_props else "Deleted" if name not in after_props else "Modified"
                    display.extend(["", f"{operation} property: {name}", *file_diff(
                        before_props.get(name, b""), after_props.get(name, b""), name,
                        "saved head", "working copy")])
            if len(display) == 2:
                display.append("No text hunks; the path's presence changed.")
            differences[value] = (action, kind, display)
        return differences

    def inspect(self):
        if self.branch not in self.state["heads"]:
            print(f"No saved head on branch {self.branch}. Use gitsvn commit first.")
            return
        head = self.state["heads"][self.branch]
        try:
            path = next((path for path in self.history(self.branch) if path.stem == head), None)
            if path is None:
                raise GitSvnError(f"Snapshot does not exist: {self.branch}/{head}")
            snapshot = self.load_snapshot(path)
            differences = self.inspect_changes(snapshot)
        except (GitSvnError, OSError, ValueError) as error:
            raise GitSvnError(f"Cannot inspect current head: {error}") from error

        if not differences:
            print("No changes compared with the saved head.")
            return
        paths = sorted(differences)
        symbols = {"added": "A", "modified": "M", "deleted": "D"}
        labels = [terminal_text(f"{symbols[differences[value][0]]}  {value}" +
                               ("/" if differences[value][1] == "dir" else ""))
                  for value in paths]
        title = terminal_text(f"Inspect {self.branch}: working copy vs saved head [{head}]")
        while True:
            selected = pick_item(labels, title, "file")
            if selected is None:
                return
            value = paths[selected]
            view_diff(f"{value} | saved head vs working copy", differences[value][2])
