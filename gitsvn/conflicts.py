"""Conflict metadata, proposed resolutions, and recovery copies."""

import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from .merge import CONFLICT_MARKERS, merge_text
from .models import Conflict, GitSvnError, Resolution


ACCEPT_METHODS = ("mine-conflict", "theirs-conflict", "mine-first", "theirs-first",
                  "mine-full", "theirs-full", "working")


def property_values(xml):
    return {node.get("name"): (base64.b64decode(node.text or "", validate=True)
                              if node.get("encoding") == "base64" else
                              (node.text or "").encode("utf-8"))
            for node in ET.fromstring(xml).findall(".//property")}


def conflicted_property_names(reject, expected):
    names = []
    in_value = False
    for line in reject.splitlines():
        if in_value:
            if line.startswith(b"<<<<<<< "):
                return []
            if line.endswith(b">>>>>>> (incoming 'changed to' value)"):
                in_value = False
            continue
        if b">>>>>>> (incoming " in line or line.startswith((b"||||||| ", b"=======")):
            return []
        header = re.fullmatch(br"Trying to (?:change|delete|add new) property '([^']+)'", line)
        if header:
            names.append(header[1].decode("utf-8"))
        if line.startswith(b"<<<<<<< "):
            in_value = True
    if (in_value or not names or len(names) != len(set(names)) or len(names) != expected):
        return []
    return names


class ConflictMixin:
    def conflicted_entries(self):
        return sorted((entry for entry in self.entries() if not entry.ignored and
                       (entry.item == "conflicted" or entry.props == "conflicted" or
                        entry.tree_conflicted)), key=lambda entry: entry.path)

    def conflict_path(self, value):
        path = Path(value)
        if path.is_absolute():
            try:
                value = str(path.relative_to(self.svn_root))
            except ValueError as error:
                raise GitSvnError("Conflict path must be inside the SVN working copy.") from error
        return str(self.safe_path(value).relative_to(self.svn_root)).replace("\\", "/")

    def load_conflict(self, entry):
        value = entry.path
        path = self.safe_path(value)
        info = ET.canonicalize(self.svn("info", "--xml", "--depth", "empty",
                                        "--", self.target(value))).encode("utf-8")
        node = ET.fromstring(info).find("entry")
        artifacts, versions = {}, {}
        for conflict in node.findall("conflict"):
            for tag in ("prev-base-file", "prev-wc-file", "cur-base-file", "prop-file"):
                name = conflict.findtext(tag)
                if name:
                    relative = self.conflict_path(name)
                    artifact = self.safe_path(relative)
                    if not artifact.is_file():
                        raise GitSvnError(f"Missing SVN conflict copy: {relative}")
                    artifacts[relative] = artifact.read_bytes()
                    if conflict.get("type") == "text":
                        versions[tag] = artifacts[relative]
        # Some SVN versions report a revision copy instead of the property reject file.
        if entry.props == "conflicted" and self.safe_path(value + ".prej").is_file():
            artifacts[value + ".prej"] = self.safe_path(value + ".prej").read_bytes()
        properties = property_values(self.svn("proplist", "--xml", "--verbose", "--depth",
                                              "empty", "--", self.target(value)))
        incoming = properties
        names = []
        if entry.props == "conflicted":
            incoming = property_values(self.svn("proplist", "--xml", "--verbose", "--depth",
                                                "empty", "-r", "BASE", "--", self.target(value)))
            reject = b"\n".join(data for name, data in artifacts.items() if name.endswith(".prej"))
            expected = sum(conflict.get("type") == "property" for conflict in node.findall("conflict"))
            text_count = sum(conflict.get("type") == "text" for conflict in node.findall("conflict"))
            # SVN 1.14 repeats each type N+1 times for a mixed conflict with N properties.
            if text_count == expected and expected > 1:
                expected -= 1
            names = conflicted_property_names(reject, expected)
        return Conflict(entry, info, path.read_bytes() if path.is_file() else None,
                        artifacts, versions, properties, incoming, names)

    def propose_resolution(self, conflict, method):
        if method not in ACCEPT_METHODS:
            raise GitSvnError(f"Unknown resolution method: {method}")
        entry = conflict.entry
        if entry.tree_conflicted and method != "working":
            raise GitSvnError("Tree conflicts need a manually prepared working state; choose working.")
        if entry.props == "conflicted" and method in ("mine-first", "theirs-first"):
            raise GitSvnError("Combining both sides is available for text-only conflicts.")
        contents = conflict.contents
        if entry.item == "conflicted" and method != "working":
            if method in ("mine-full", "theirs-full"):
                tag = "prev-wc-file" if method == "mine-full" else "cur-base-file"
                contents = conflict.versions.get(tag)
                mime_type = conflict.properties.get("svn:mime-type", b"text/plain")
                binary = conflict.contents is not None and (b"\x00" in conflict.contents or
                                                            not mime_type.startswith(b"text/"))
                if contents is None and method == "mine-full" and binary:
                    contents = conflict.contents
                if contents is None:
                    raise GitSvnError("The chosen whole-file conflict copy is unavailable.")
            else:
                contents = merge_text(contents, method)
        if contents is not None and CONFLICT_MARKERS.search(contents):
            raise GitSvnError("Conflict markers remain. Finish editing the file before accepting it.")
        properties = dict(conflict.properties)
        if entry.props == "conflicted" and method in ("theirs-conflict", "theirs-full"):
            if not conflict.property_names:
                raise GitSvnError("Cannot identify conflicted properties; edit them and choose working.")
            for name in conflict.property_names:
                if name in conflict.incoming_properties:
                    properties[name] = conflict.incoming_properties[name]
                else:
                    properties.pop(name, None)
        return Resolution(method, contents, properties)

    def backup_conflict(self, conflict, resolution):
        directory = self.state_dir / "resolutions"
        directory.mkdir(parents=True, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d_%H%M%S_%f_"),
                                       dir=directory))
        files = {}
        if conflict.contents is not None:
            (backup / "working-file").write_bytes(conflict.contents)
            files[conflict.entry.path] = "working-file"
        for index, (name, data) in enumerate(conflict.artifacts.items(), 1):
            filename = f"artifact-{index}"
            (backup / filename).write_bytes(data)
            files[name] = filename
        (backup / "conflict.xml").write_bytes(conflict.info)
        properties = {name: base64.b64encode(data).decode("ascii")
                      for name, data in conflict.properties.items()}
        (backup / "properties.json").write_text(json.dumps(properties, indent=2), encoding="utf-8")
        (backup / "manifest.json").write_text(json.dumps(
            {"version": 1, "path": conflict.entry.path, "method": resolution.method,
             "files": files}, indent=2), encoding="utf-8")
        return backup

    def apply_resolution(self, conflict, resolution):
        fresh = self.load_conflict(conflict.entry)
        if fresh != conflict:
            raise GitSvnError("The conflict changed after the preview; review it again before applying.")
        backup = self.backup_conflict(conflict, resolution)
        temporary = None
        try:
            if resolution.contents is not None and resolution.contents != conflict.contents:
                path = self.safe_path(conflict.entry.path)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(resolution.contents)
            for name in sorted(set(conflict.properties) | set(resolution.properties)):
                if conflict.properties.get(name) == resolution.properties.get(name):
                    continue
                if name not in resolution.properties:
                    self.svn("propdel", "--", name, self.target(conflict.entry.path))
                else:
                    property_file = backup / "property-value"
                    property_file.write_bytes(resolution.properties[name])
                    self.svn("propset", "--file", str(property_file), "--", name,
                             self.target(conflict.entry.path))
            if temporary is not None:
                os.replace(temporary, self.safe_path(conflict.entry.path))
                temporary = None
            output = self.svn("resolve", "--accept", "working", "--depth", "empty",
                              "--", self.target(conflict.entry.path))
            remaining = self.conflicted_entries()
            if any(entry.path == conflict.entry.path for entry in remaining):
                raise GitSvnError("SVN still reports a conflict on this path.")
        except (GitSvnError, OSError, ValueError, ET.ParseError, KeyboardInterrupt) as error:
            raise GitSvnError(f"Resolution did not finish: {error}\nRecovery copies: {backup}") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if output.strip():
            print(output.decode("utf-8", errors="replace").rstrip())
        print(f"Resolved {conflict.entry.path}. Recovery copies: {backup}")
