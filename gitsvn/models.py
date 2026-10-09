"""Shared errors and snapshot records."""

from dataclasses import dataclass


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
