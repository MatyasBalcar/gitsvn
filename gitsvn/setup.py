"""Guided configuration and user PATH registration."""

from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

from . import config as configuration
from .app import GitSvn
from .config import configuration_path, load_config, resolve_path, save_json
from .models import GitSvnError
from .working_copy import detect_working_copy_root, working_copy_root


def validate_folder(path):
    for parent in (path, *path.parents):
        if parent.exists():
            if not parent.is_dir():
                raise GitSvnError(f"Not a folder: {parent}")
            return
    raise GitSvnError(f"Drive or parent folder does not exist: {path}")


def prompt_path(label, default, validate):
    while True:
        choice = input(f"{label} [{default}]: ").strip()
        try:
            if len(choice) >= 2 and choice[0] == choice[-1] and choice[0] in ("'", '"'):
                choice = choice[1:-1]
            path = Path(choice).resolve() if choice else default
            validate(path)
            return path
        except (GitSvnError, OSError, ValueError, ET.ParseError) as error:
            print(f"Invalid {label.lower()}: {error}")


def prompt_autosave(default):
    while True:
        choice = input(f"Autosave when switching (true/false) [{str(default).lower()}]: ").strip().lower()
        if not choice:
            return default
        if choice in ("true", "yes", "y"):
            return True
        if choice in ("false", "no", "n"):
            return False
        print("Enter true or false (yes/no also works).")


def register_path():
    installer = configuration.program_directory() / "install.ps1"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                             "-File", str(installer)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise GitSvnError(message or f"Installer exited with code {result.returncode}.")
    if result.stdout:
        print(result.stdout.decode(errors="replace").rstrip())


def initialize(arguments):
    program_dir = configuration.program_directory()
    for name in ("main.py", "gitsvn.cmd", "install.ps1",
                 "gitsvn/__init__.py", "gitsvn/models.py", "gitsvn/config.py",
                 "gitsvn/terminal.py", "gitsvn/working_copy.py", "gitsvn/snapshots.py",
                 "gitsvn/inspection.py", "gitsvn/branches.py", "gitsvn/app.py",
                 "gitsvn/conflicts.py", "gitsvn/conflict_ui.py", "gitsvn/merge.py",
                 "gitsvn/setup.py", "gitsvn/cli.py"):
        if not (program_dir / name).is_file():
            raise GitSvnError(f"Missing gitsvn file: {program_dir / name}")
    for name in ("python", "svn"):
        if shutil.which(name) is None:
            raise GitSvnError(f"{name} must be available on PATH before initialization.")
    config_path = configuration_path(arguments.state_dir, arguments.config)
    if config_path.name.casefold() == "state.json":
        raise GitSvnError("state.json is reserved for branch state; choose a separate config file.")
    validate_folder(config_path.parent)
    config = load_config(config_path)
    default_root = configuration.DEFAULT_SVN_ROOT
    if arguments.svn_root is None and "svn_root" not in config:
        try:
            default_root = detect_working_copy_root(Path.cwd())
        except GitSvnError:
            pass
    defaults = {
        "svn_root": resolve_path(arguments.svn_root, "svn_root", default_root, config, config_path),
        "patch_root": resolve_path(arguments.patch_root, "patch_root", configuration.DEFAULT_PATCH_ROOT,
                                   config, config_path),
        "state_dir": resolve_path(arguments.state_dir, "state_dir", config_path.parent,
                                  config, config_path),
    }

    def validate_root(path):
        validate_folder(path)
        actual = working_copy_root(path)
        if actual != path:
            raise GitSvnError(f"Select the SVN working-copy root: {actual}")

    def validate_patch(path):
        validate_folder(path)
        if path.is_relative_to(Path(config["svn_root"])):
            raise GitSvnError("Patch storage must be outside the SVN working copy.")

    def validate_state(path):
        validate_folder(path)
        # Reuse the normal workspace/state guards without writing any files.
        GitSvn(config_path=config_path, _config={**config, "state_dir": str(path)})

    print("Initialize gitsvn. Press Enter to keep each default; Ctrl+C cancels.")
    print(f"Configuration: {config_path}")
    try:
        config["svn_root"] = str(prompt_path("SVN working-copy root", defaults["svn_root"], validate_root))
        config["patch_root"] = str(prompt_path("Patch folder", defaults["patch_root"], validate_patch))
        config["state_dir"] = str(prompt_path("State folder", defaults["state_dir"], validate_state))
        config["autosave"] = prompt_autosave(config["autosave"])
    except EOFError:
        print("Initialization cancelled.")
        return 0

    Path(config["patch_root"]).mkdir(parents=True, exist_ok=True)
    Path(config["state_dir"]).mkdir(parents=True, exist_ok=True)
    save_json(config_path, config)
    print(f"Saved configuration: {config_path}")
    if config_path != configuration_path():
        print(f'Use this configuration with: gitsvn --config "{config_path}" <command>')
    try:
        register_path()
    except (GitSvnError, OSError) as error:
        raise GitSvnError(
            f"Configuration saved, but PATH registration failed: {error}\n"
            f'Retry with: powershell -NoProfile -ExecutionPolicy Bypass -File "{program_dir / "install.ps1"}"'
        ) from error
    print("Initialization complete. Open a new terminal to use gitsvn from any directory.")
    return 0
