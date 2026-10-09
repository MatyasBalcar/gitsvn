"""Configuration paths, defaults, and atomic JSON storage."""

import json
import os
from pathlib import Path
import tempfile

from .models import GitSvnError


DEFAULT_SVN_ROOT = Path(r"D:\Elektlabs")
DEFAULT_PATCH_ROOT = Path(r"D:\Patches")


def program_directory():
    return Path(__file__).resolve().parent.parent


def configuration_path(state_dir=None, config_path=None):
    if config_path is not None:
        return Path(config_path).resolve()
    directory = (Path(state_dir).resolve() if state_dir is not None
                 else program_directory() / ".gitsvn")
    return directory / "config.json"


def load_config(path, required=False):
    if required and not path.is_file():
        raise GitSvnError(f"Config file does not exist: {path}")
    config = {"autosave": True}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(settings, dict) or not isinstance(settings.get("autosave", True), bool):
                raise ValueError("autosave must be true or false")
            for key in ("svn_root", "patch_root", "state_dir"):
                if key in settings and (not isinstance(settings[key], str) or not settings[key].strip()):
                    raise ValueError(f"{key} must be a nonempty path string")
            config.update(settings)
        except (ValueError, TypeError) as error:
            raise GitSvnError(f"Invalid config file: {path}: {error}") from error
    return config


def resolve_path(value, key, default, config, config_path):
    if value is not None:
        return Path(value).resolve()
    if key not in config:
        return Path(default).resolve()
    path = Path(config[key])
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
