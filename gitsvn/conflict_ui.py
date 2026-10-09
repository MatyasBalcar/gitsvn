"""Conflict pickers and review before applying a proposed resolution."""

import difflib
import hashlib

from .models import GitSvnError
from .terminal import pick_item, terminal_text, view_diff


def file_diff(before, after, title):
    if b"\x00" in before or b"\x00" in after:
        return [title, f"Before: {len(before)} bytes, SHA256 {hashlib.sha256(before).hexdigest()}",
                f"After:  {len(after)} bytes, SHA256 {hashlib.sha256(after).hexdigest()}"]
    details = []
    if before.startswith(b"\xef\xbb\xbf") != after.startswith(b"\xef\xbb\xbf"):
        details.append(f"UTF-8 BOM: {'present' if after.startswith(b'\xef\xbb\xbf') else 'absent'} in result")
    if before.endswith(b"\n") != after.endswith(b"\n"):
        details.append(f"Final newline: {'present' if after.endswith(b'\n') else 'absent'} in result")
    before_crlf, after_crlf = before.count(b"\r\n"), after.count(b"\r\n")
    if (bool(before_crlf), b"\n" in before.replace(b"\r\n", b"")) != \
            (bool(after_crlf), b"\n" in after.replace(b"\r\n", b"")):
        details.append(f"Line endings: CRLF={after_crlf}, LF={after.count(b'\n') - after_crlf} in result")
    return details + list(difflib.unified_diff(before.decode("utf-8-sig", errors="backslashreplace").splitlines(),
                                     after.decode("utf-8-sig", errors="backslashreplace").splitlines(),
                                     fromfile=title + " (before)", tofile=title + " (proposed)",
                                     lineterm=""))


def property_lines(properties):
    return [f"{name} = {value!r}" for name, value in sorted(properties.items())]


class ConflictUiMixin:
    def review_conflict(self, conflict):
        lines = []
        base = conflict.versions.get("prev-base-file", b"")
        for name, tag in (("My changes", "prev-wc-file"), ("Incoming changes", "cur-base-file")):
            if tag in conflict.versions:
                lines.extend([name, *file_diff(base, conflict.versions[tag], name), ""])
        if conflict.entry.props == "conflicted":
            for name, contents in conflict.artifacts.items():
                if name.endswith(".prej"):
                    lines.extend(["Property conflict:", *contents.decode("utf-8", errors="replace").splitlines()])
        if conflict.entry.tree_conflicted:
            lines.extend(["Tree conflict: prepare the intended working state before marking resolved.",
                          *conflict.info.decode("utf-8", errors="replace").splitlines()])
        view_diff(f"Conflict: {conflict.entry.path}", lines or ["No text conflict copies available."])

    def review_resolution(self, conflict, resolution):
        lines = [f"Proposed resolution: {resolution.method}", ""]
        if resolution.contents is not None:
            lines.extend(file_diff(conflict.contents or b"", resolution.contents, "Working file"))
            incoming = conflict.versions.get("cur-base-file")
            if incoming is not None:
                lines.extend(["", *file_diff(incoming, resolution.contents, "Incoming vs result")])
        lines.extend(difflib.unified_diff(property_lines(conflict.properties),
                                         property_lines(resolution.properties),
                                         fromfile="Current properties", tofile="Proposed properties",
                                         lineterm=""))
        if conflict.entry.tree_conflicted:
            lines.extend(["Mark the current working state resolved for this path only.",
                          *conflict.info.decode("utf-8", errors="replace").splitlines()])
        if len(lines) == 2:
            lines.append("Keep the manually prepared file and property values, then mark resolved.")
        view_diff(f"Review before applying: {conflict.entry.path}", lines)
        return pick_item(["Back (leave the conflict unchanged)", "Apply resolution"],
                         f"Apply {resolution.method} to {terminal_text(conflict.entry.path)}?", "action") == 1

    def resolve(self, path=None, accept=None):
        if accept is not None and path is None:
            raise GitSvnError("--accept requires a conflict path; the result is still reviewed before applying.")
        while True:
            entries = self.conflicted_entries()
            if not entries:
                if path is not None:
                    value = self.conflict_path(path)
                    raise GitSvnError(f"No resolvable conflict on {value}; ignore-on-commit paths are excluded.")
                print("No unresolved conflicts.")
                return
            if path is not None:
                value = self.conflict_path(path)
                entry = next((entry for entry in entries if entry.path == value), None)
                if entry is None:
                    raise GitSvnError(f"No resolvable conflict on {value}; ignore-on-commit paths are excluded.")
            else:
                labels = [f"{entry.path} ({', '.join(kind for kind, present in (
                    ('text', entry.item == 'conflicted'), ('properties', entry.props == 'conflicted'),
                    ('tree', entry.tree_conflicted)) if present)})" for entry in entries]
                selected = pick_item(labels, "Resolve conflict", "file")
                if selected is None:
                    return
                entry = entries[selected]
            conflict = self.load_conflict(entry)
            while True:
                method = accept
                if method is None:
                    methods = [None, "mine-conflict", "theirs-conflict", "mine-first", "theirs-first",
                               "mine-full", "theirs-full", "working"]
                    labels = ["Review my and incoming changes",
                              "Keep my conflicting lines (keep other merged changes)",
                              "Use incoming conflicting lines (keep other merged changes)",
                              "My lines before incoming lines in each conflict",
                              "Incoming lines before my lines in each conflict",
                              "Use my whole file (discard incoming file changes)",
                              "Use incoming whole file (discard local file changes)",
                              "Accept my manually edited result"]
                    if entry.tree_conflicted:
                        methods, labels = [None, "working"], [labels[0], labels[-1]]
                    elif entry.props == "conflicted":
                        methods = [None, "mine-conflict", "theirs-conflict", "working"]
                        labels = [labels[0], "Keep my conflicted text/property values",
                                  "Use incoming conflicted text/property values", labels[-1]]
                    selected = pick_item(labels, f"Resolve {terminal_text(entry.path)}", "method")
                    if selected is None:
                        break
                    method = methods[selected]
                    if method is None:
                        self.review_conflict(conflict)
                        continue
                try:
                    resolution = self.propose_resolution(conflict, method)
                except GitSvnError as error:
                    if accept is not None:
                        raise
                    print(f"gitsvn: {error}")
                    continue
                if self.review_resolution(conflict, resolution):
                    self.apply_resolution(conflict, resolution)
                    break
                if accept is not None:
                    return
            if path is not None:
                return
