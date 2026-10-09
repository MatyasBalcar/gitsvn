"""Resolve ordinary SVN text conflict blocks without changing other bytes."""

import re

from .models import GitSvnError


CONFLICT_MARKERS = re.compile(br"(?m)^(?:<{7}|\|{7}|>{7})(?:[ \t]|\r?$)")


def merge_text(contents, method):
    if b"\x00" in contents:
        raise GitSvnError("Choose a whole-file version for a binary conflict.")
    output = bytearray()
    mine, theirs = bytearray(), bytearray()
    state = "context"
    count = 0
    block_start = 0
    for line in contents.splitlines(keepends=True):
        marker = line.rstrip(b"\r\n")
        if state != "context" and any(match.start() for match in re.finditer(
                br"(?:<{7}|\|{7}|>{7}|={7})(?:[ \t]|$)", marker)):
            raise GitSvnError("Conflict delimiter is attached to an unterminated line; choose a whole file.")
        opening = re.fullmatch(br"<{7}(?:[ \t].*)?", marker)
        base = re.fullmatch(br"\|{7}(?:[ \t].*)?", marker)
        closing = re.fullmatch(br">{7}(?:[ \t].*)?", marker)
        separator = marker == b"======="
        if state == "context":
            if opening:
                mine, theirs = bytearray(), bytearray()
                block_start = len(output)
                state = "mine"
            elif base or closing:
                raise GitSvnError("Malformed SVN conflict markers; edit manually or choose a whole file.")
            else:
                output.extend(line)
        elif opening:
            raise GitSvnError("Nested SVN conflict markers; edit manually or choose a whole file.")
        elif state == "mine" and base:
            state = "base"
        elif state in ("mine", "base") and separator:
            state = "theirs"
        elif state == "theirs" and closing:
            if method == "mine-conflict":
                output.extend(mine)
            elif method == "theirs-conflict":
                output.extend(theirs)
            else:
                first, second = (mine, theirs) if method == "mine-first" else (theirs, mine)
                output.extend(first)
                # Both first lines can carry a BOM; keep just the leading one.
                duplicate_bom = block_start == 0 and first.startswith(b"\xef\xbb\xbf")
                output.extend(bytes(second).removeprefix(b"\xef\xbb\xbf") if duplicate_bom else second)
            state = "context"
            count += 1
        elif base or closing or separator:
            raise GitSvnError("Malformed SVN conflict markers; edit manually or choose a whole file.")
        elif state == "mine":
            mine.extend(line)
        elif state == "theirs":
            theirs.extend(line)
    if state != "context":
        raise GitSvnError("Incomplete SVN conflict block; edit manually or choose a whole file.")
    if not count:
        raise GitSvnError("No complete conflict blocks remain; choose the manually edited result.")
    return bytes(output)
