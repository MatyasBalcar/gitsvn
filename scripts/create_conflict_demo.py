"""Create a local SVN conflict and leave it unresolved for hands-on testing."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


PROGRAM_DIR = Path(__file__).resolve().parents[1]


def run(arguments, directory, *, expect_conflict=False):
    result = subprocess.run(arguments, cwd=directory, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode and not expect_conflict:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result


def create_demo():
    for command in ("svn", "svnadmin"):
        if shutil.which(command) is None:
            raise RuntimeError(f"{command} must be available on PATH.")
    parent = PROGRAM_DIR / "demo-conflicts"
    parent.mkdir(exist_ok=True)
    demo = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d_%H%M%S_"), dir=parent))
    if demo.parent.resolve() != parent.resolve():
        raise RuntimeError("Demo folder must stay inside demo-conflicts.")
    repository, working, incoming = (demo / name for name in ("repository", "working-copy", "incoming"))
    run(["svnadmin", "create", str(repository)], demo)
    seed = demo / "seed" / "sql"
    seed.mkdir(parents=True)
    original = [f"-- Demo context line {number:02d}\n" for number in range(1, 36)]
    original[1] = "-- Developer comment: original\n"
    original[17] = "UPDATE DemoSettings SET UpgradeMode = 'original';\n"
    original[32] = "-- Deployment comment: original\n"
    name = "sql/141_demo.sql"
    (seed / "141_demo.sql").write_bytes("".join(original).encode("utf-8"))
    url = repository.as_uri()
    run(["svn", "import", str(seed.parent), url, "-m", "Demo baseline"], demo)
    for checkout in (working, incoming):
        run(["svn", "checkout", url, str(checkout)], demo)
    config = demo / "config.json"
    config.write_text(json.dumps({
        "svn_root": str(working), "patch_root": str(demo / "patches"),
        "state_dir": str(demo / "state"), "autosave": True,
    }, indent=2) + "\n", encoding="utf-8")
    cli = [sys.executable, str(PROGRAM_DIR / "main.py"), "--config", str(config)]
    run([*cli, "branch", "demo141"], demo)
    run([*cli, "switch", "demo141"], demo)

    local, deployed = original.copy(), original.copy()
    local[1] = "-- Developer comment: keep my local work\n"
    local[17] = "UPDATE DemoSettings SET UpgradeMode = 'my-development-version';\n"
    deployed[17] = "UPDATE DemoSettings SET UpgradeMode = 'incoming-deployed-version';\n"
    deployed[32] = "-- Deployment comment: keep the incoming deployment change\n"
    (working / name).write_bytes("".join(local).encode("utf-8"))
    run([*cli, "commit", "-m", "Demo: local upgrade work before deployment"], demo)
    (incoming / name).write_bytes("".join(deployed).encode("utf-8"))
    run(["svn", "commit", "-m", "Demo: deployed upgrade change"], incoming)
    run([*cli, "pull"], demo, expect_conflict=True)
    # Include an edit made after the conflict, to exercise preserving later work.
    script = working / name
    script.write_bytes(script.read_bytes().replace(
        b"-- Demo context line 07", b"-- Edited AFTER update: keep this too"))
    status = ET.fromstring(run(["svn", "status", "--xml", name], working).stdout)
    if not any(node.get("item") == "conflicted" for node in status.findall(".//wc-status")):
        raise RuntimeError(f"SVN did not create the expected conflict. Demo files: {demo}")

    launcher = '@echo off\npython "' + str(PROGRAM_DIR / "main.py") + '" --config "%~dp0config.json" '
    (demo / "resolve.cmd").write_text(launcher + 'resolve %*\nexit /b %errorlevel%\n', encoding="utf-8")
    (demo / "demo.cmd").write_text(launcher + '%*\nexit /b %errorlevel%\n', encoding="utf-8")
    (demo / "README.txt").write_text(
        "A real SVN update conflict is waiting in working-copy/sql/141_demo.sql.\n"
        "Run resolve.cmd to open the resolver using this demo's own configuration.\n"
        "Run demo.cmd status or demo.cmd inspect to use other commands in this demo.\n"
        "Local and incoming comments are separate changes; the UPDATE line is the conflict.\n"
        "An additional developer comment was edited after the update.\n"
        "Preview each choice and select Back until you are ready to apply it.\n"
        "Run scripts/create_conflict_demo.py again to create another independent conflict.\n"
        "The SQL file is a text fixture and has not been executed.\n",
        encoding="utf-8")
    print(f"Unresolved conflict ready: {script}")
    print(f"Open the resolver yourself: {demo / 'resolve.cmd'}")
    print(f"Other commands: {demo / 'demo.cmd'} status")
    print("No resolution has been applied; your normal configuration was not changed.")
    return demo


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolve", action="store_true",
                        help="Open the resolver in this terminal after creating the conflict")
    arguments = parser.parse_args()
    try:
        demo = create_demo()
        if arguments.resolve:
            sys.stdout.flush()
            result = subprocess.run([sys.executable, str(PROGRAM_DIR / "main.py"),
                                     "--config", str(demo / "config.json"), "resolve"], cwd=demo)
            sys.exit(result.returncode)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ET.ParseError) as error:
        print(f"Demo setup failed: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
