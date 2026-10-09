"""Saved-head comparison, branch status, and snapshot inspection."""

import base64

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
            sections = self.snapshot_sections(snapshot)
        except (GitSvnError, OSError, ValueError) as error:
            raise GitSvnError(f"Cannot inspect current head: {error}") from error

        metadata = {entry["path"].replace("\\", "/"): entry for entry in snapshot.entries}
        for value, details in metadata.items():
            item = "modified" if details["item"] == "normal" else details["item"]
            sections[value] = (item, sections.get(value, (item, ()))[1])
        if not sections:
            print("No changed files in current version.")
            return
        paths = sorted(sections)
        symbols = {"added": "A", "modified": "M", "deleted": "D"}
        labels = [terminal_text(f"{symbols[sections[value][0]]}  {value}" +
                               ("/" if metadata.get(value, {}).get("kind") == "dir" else ""))
                  for value in paths]
        title = terminal_text(f"Inspect {self.branch}: {self.snapshot_label(path)} (current version)")
        while True:
            selected = pick_item(labels, title, "file")
            if selected is None:
                return
            value = paths[selected]
            item, contents = sections[value]
            details = metadata.get(value, {})
            kind = "directory" if details.get("kind") == "dir" else "file"
            lines = [f"{item.capitalize()} {kind}: {value}", ""]
            lines.extend(line.decode("utf-8-sig", errors="replace").rstrip("\r\n")
                         for line in contents)
            if not contents:
                lines.append("No text hunks; this snapshot records the path change only.")
            view_diff(f"{value} | {self.branch}/{snapshot.name}", lines)
