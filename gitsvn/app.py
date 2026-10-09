"""Workspace state and the public gitsvn command interface."""

import json
from pathlib import Path
import re

from . import config as configuration
from .branches import BranchMixin
from .config import configuration_path, load_config, resolve_path, save_json
from .inspection import InspectionMixin
from .models import GitSvnError
from .snapshots import SnapshotMixin
from .working_copy import WorkingCopyMixin


class GitSvn(InspectionMixin, BranchMixin, SnapshotMixin, WorkingCopyMixin):
    def __init__(self, svn_root=None, patch_root=None, state_dir=None, config_path=None, *, _config=None):
        self.config_path = configuration_path(state_dir, config_path)
        # Init validates chosen settings before writing a new config file.
        self.config = (load_config(self.config_path, required=config_path is not None)
                       if _config is None else {"autosave": True, **_config})
        self.svn_root = self.configured_path(svn_root, "svn_root", configuration.DEFAULT_SVN_ROOT)
        self.patch_root = self.configured_path(patch_root, "patch_root", configuration.DEFAULT_PATCH_ROOT)
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
        return resolve_path(value, key, default, self.config, self.config_path)

    @property
    def branch(self):
        return self.state["branch"]

    def save_state(self):
        save_json(self.state_path, self.state)

    def branch_path(self, name):
        if (not isinstance(name, str) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or
                name.endswith(".") or
                re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name)):
            raise GitSvnError("Invalid branch name; use letters, numbers, dots, '-' or '_'.")
        path = (self.patch_root / name).resolve()
        if path.parent != self.patch_root:
            raise GitSvnError("Branch folder must stay inside patch storage.")
        program_dir = configuration.program_directory()
        if (self.state_dir.is_relative_to(path) or
                (program_dir / "main.py").is_relative_to(path) or
                (program_dir / "gitsvn").is_relative_to(path)):
            raise GitSvnError("That folder is reserved for gitsvn itself.")
        return path
