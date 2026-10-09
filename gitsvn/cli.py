"""Command-line arguments, dispatch, and error reporting."""

import argparse
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from .app import GitSvn
from .models import GitSvnError
from .setup import initialize


def parser():
    result = argparse.ArgumentParser(prog="gitsvn", description="Local Git-like patch branches for SVN.")
    result.add_argument("--config", help="Settings file; defaults to .gitsvn/config.json beside the program")
    result.add_argument("--svn-root", help="SVN working-copy root; overrides config")
    result.add_argument("--patch-root", help="Patch storage folder; overrides config")
    result.add_argument("--state-dir", help="State folder; also selects config unless --config is supplied")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Configure folders and autosave, then add gitsvn to user PATH")
    branch = commands.add_parser("branch", help="List branches or initialize a clean-trunk branch")
    branch.add_argument("name", nargs="?")
    switch = commands.add_parser("switch", help="Restore a branch using the autosave setting")
    switch.add_argument("name", nargs="?", help="Branch to restore; omit to open the branch picker")
    commit = commands.add_parser("commit", help="Save a timestamped patch and message")
    commit.add_argument("-m", "--message", required=True)
    finalize = commands.add_parser("finalize", help="Export a date/description-named ticket patch")
    finalize.add_argument("description")
    finalize.add_argument("ticket", nargs="?", help="Destination ticket; defaults to current branch")
    finalize.add_argument("--latest", action="store_true", help="Export the branch's saved snapshot")
    log = commands.add_parser("log", help="List snapshot timestamps and messages")
    log.add_argument("branch", nargs="?")
    commands.add_parser("inspect", help="Browse file diffs in the current branch's saved head")
    revert = commands.add_parser("revert", help="Choose a saved snapshot to restore")
    revert.add_argument("branch", nargs="?")
    revert.add_argument("snapshot", nargs="?", help="Snapshot ID; omit to open the snapshot picker")
    commands.add_parser("pull", help="Autosave changes and run svn update")
    commands.add_parser("status", help="Show the current branch's tracked changes")
    add = commands.add_parser("add", help="Schedule working-copy paths with svn add")
    add.add_argument("paths", nargs="+")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "init":
            return initialize(arguments)
        app = GitSvn(arguments.svn_root, arguments.patch_root, arguments.state_dir, arguments.config)
        if (arguments.command in ("switch", "commit", "revert", "pull", "status", "add") or
                (arguments.command == "finalize" and not arguments.latest)):
            app.verify_working_copy()
        if arguments.command == "branch":
            app.create_branch(arguments.name) if arguments.name else app.list_branches()
        elif arguments.command == "switch":
            app.switch(arguments.name)
        elif arguments.command == "commit":
            app.commit(arguments.message)
        elif arguments.command == "finalize":
            app.finalize(arguments.description, arguments.ticket, arguments.latest)
        elif arguments.command == "log":
            app.list_history(arguments.branch or app.branch)
        elif arguments.command == "inspect":
            app.inspect()
        elif arguments.command == "revert":
            app.select_restore(arguments.branch or app.branch, arguments.snapshot)
        elif arguments.command == "pull":
            app.pull()
        elif arguments.command == "status":
            app.status()
        elif arguments.command == "add":
            for path in arguments.paths:
                full_path = Path(path)
                value = str(full_path.relative_to(app.svn_root)) if full_path.is_absolute() else path
                app.safe_path(value)
                print(app.svn("add", "--", app.target(value)).decode("utf-8", errors="replace"),
                      end="")
        return 0
    except (GitSvnError, OSError, ValueError, ET.ParseError) as error:
        print(f"gitsvn: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("gitsvn: Interrupted.", file=sys.stderr)
        return 130
