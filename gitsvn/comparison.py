"""Reconstruct saved text and properties from SVN unified patch hunks."""

import re

from .models import GitSvnError


def apply_hunks(contents, lines, marker=b"@@", normalize_eol=False):
    if normalize_eol:
        contents = contents.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    original = contents.splitlines(keepends=True)
    hunks = []
    for line in lines:
        header = re.match(re.escape(marker) + br" -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? " +
                          re.escape(marker), line)
        if header:
            old_count = int(header[2]) if header[2] is not None else 1
            new_count = int(header[4]) if header[4] is not None else 1
            start = int(header[1]) - (1 if old_count else 0)
            hunks.append((start, old_count, new_count, []))
        elif hunks and line.startswith(b"\\"):
            if not hunks[-1][3]:
                raise GitSvnError("Invalid newline marker in saved patch.")
            hunks[-1][3][-1] = hunks[-1][3][-1].rstrip(b"\r\n")
        elif hunks and line.startswith((b" ", b"-", b"+")):
            hunks[-1][3].append(line)
    result = []
    position = 0
    for start, old_count, new_count, payload in hunks:
        if start < position or start > len(original):
            raise GitSvnError("Saved patch no longer matches SVN BASE.")
        result.extend(original[position:start])
        position = start
        removed = added = 0
        for line in payload:
            prefix, data = line[:1], line[1:]
            if normalize_eol:
                data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            if prefix in (b" ", b"-"):
                if position >= len(original) or original[position] != data:
                    raise GitSvnError("Saved patch no longer matches SVN BASE.")
                position += 1
                removed += 1
            if prefix in (b" ", b"+"):
                result.append(data)
                added += 1
        if (removed, added) != (old_count, new_count):
            raise GitSvnError("Incomplete saved patch hunk.")
    result.extend(original[position:])
    return b"".join(result)


def saved_changes(contents, properties, lines):
    text = []
    property_changes = {}
    target = text
    for line in lines:
        header = re.fullmatch(br"(Added|Modified|Deleted): (.+)", line.rstrip(b"\r\n"))
        if line == b"Property changes on:":
            target = []
        elif header:
            name = header[2].decode("utf-8")
            target = []
            property_changes[name] = (header[1], target)
        else:
            target.append(line)
    saved = dict(properties)
    for name, (action, hunks) in property_changes.items():
        if action == b"Deleted":
            saved.pop(name, None)
        else:
            saved[name] = apply_hunks(properties.get(name, b""), hunks, b"##")
    normalize = bool(properties.get("svn:eol-style") or saved.get("svn:eol-style"))
    return apply_hunks(contents, text, normalize_eol=normalize), saved
