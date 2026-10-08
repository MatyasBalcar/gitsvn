# gitsvn

A local, Git-like workflow for an SVN working copy. Save changes as patches, organize them into local branches, switch between saved versions, and export a final patch for a ticket.

Local commits are snapshots of your uncommitted SVN changes. Publishing changes to the SVN server remains a separate step using your usual SVN client.

## Setup

### Requirements

- Windows with Python 3.12 or newer.
- `python` and `svn` available on PATH.
- An existing SVN working copy.

### Guided setup

Keep `main.py`, `gitsvn.cmd`, and `install.ps1` together in the program folder. From that folder, run:

```powershell
.\gitsvn.cmd init
```

If `gitsvn` is already on PATH, run `gitsvn init` from any directory. Setup asks for the SVN working-copy root, patch folder, state folder, and autosave setting. **Press Enter to keep each displayed default.** On a first run, it detects the working-copy root from your current checkout when possible; otherwise, enter your checkout's root when prompted. Patch storage defaults to `D:\Patches`, state to `.gitsvn` beside the program, and autosave to `true`.

Setup validates the folders, creates patch and state storage, saves the configuration, and registers the program in your user PATH. **Open a new terminal afterward.** Re-running setup uses your existing settings as defaults, preserving preferences such as `autosave: false`. Relative paths you enter are resolved from the current directory and saved as absolute paths. During the prompts, Ctrl+C cancels without saving settings.

Existing branch state and patches are preserved. When changing the working copy or patch root, choose a separate state folder if the current one belongs to another workspace. Setup creates no commits or initial branch snapshots.

Existing global options also work with `init`, for example:

```powershell
.\gitsvn.cmd --config C:\Work\another-project.json init
```

Unlike other commands, `init` can create a missing configuration file selected with `--config`. A nondefault configuration still needs `--config` on subsequent commands; setup prints the invocation. If PATH registration fails after saving, the configuration remains available and the error explains how to retry the installer.

The sections below describe the folder choices and manual setup alternative.

### Choose the folders

The program can live on any drive, for example `C:\Tools\gitsvn`. Keep `main.py`, `gitsvn.cmd`, and `install.ps1` together. Choose a writable folder, or configure a separate writable state folder.

Patch storage defaults to **`D:\Patches`**. Your PC needs a `D:` drive to use that default. The first save creates the folder and its branch subfolders automatically; you can also create it yourself:

```powershell
New-Item -ItemType Directory -Force -Path D:\Patches
```

The program does not have to live inside the patch folder. If you do not have a `D:` drive, or prefer another location, set `patch_root` to that location in the configuration below.

Both patch storage and local state must be **outside the SVN working copy**. By default, state and configuration live in `.gitsvn` beside `main.py`. If you keep the program inside an SVN checkout, configure an external `state_dir`.

### Make the command available everywhere

From the folder containing the program, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Open a new terminal afterward. The installer adds the program's actual location to your user PATH, so `gitsvn` works from any directory. If you move the program later, update its PATH entry. You can also run `gitsvn.cmd` directly by its full path.

### Select your working copy

Create or edit `.gitsvn\config.json` beside `main.py` once. Set `svn_root` to the SVN working-copy root, the directory containing `.svn`, and choose your patch and state folders:

```json
{
  "svn_root": "D:\\Source\\ExampleProject",
  "patch_root": "D:\\Patches",
  "state_dir": "C:\\Work\\gitsvn-state",
  "autosave": true
}
```

Windows paths in JSON use doubled backslashes. Then run plain commands from any directory:

```powershell
gitsvn status
```

The configuration is found beside the program, regardless of your current directory. Use a separate state folder for each working copy and patch root; state records those paths and refuses mismatches. See [Configuration](#configuration) for optional command-line overrides.

## Everyday workflow

After configuring your folders:

```powershell
gitsvn branch 12345
gitsvn switch 12345

# Edit files in the SVN working copy. Add new files before saving them.
# Edited files dont need to be added, just created ones 
gitsvn add src\Example.cs
gitsvn commit -m "Fix input validation"

gitsvn log
gitsvn inspect
gitsvn finalize FixInputValidation
gitsvn switch trunk
```

Creating a branch saves an empty initial snapshot of SVN BASE and leaves your current edits in place. `switch` restores that branch's saved version. Each commit contains the complete diff against SVN BASE, so restoring a version applies one snapshot rather than a sequence of commits.

## Commands

Use `gitsvn [options] <command>`:

| Command | What it does |
| --- | --- |
| `init` | Configure folders and autosave interactively, then register gitsvn in your user PATH. Enter keeps each default. |
| `branch` | List local branches; `*` marks the current branch. |
| `branch NAME` | Create or locate a branch folder and initialize it when needed. |
| `switch [NAME]` | Restore the branch's saved head, optionally saving outgoing changes according to `autosave`. Omit the name to open the branch picker. |
| `status` | Show the current branch's tracked changes. A is green, M yellow, and D red; saved filenames are white and unsaved filenames red. |
| `add PATH...` | Schedule new files or folders with SVN. Paths are relative to the working-copy root. |
| `commit -m MESSAGE` | Save a timestamped patch, message, and snapshot metadata in the current branch folder. |
| `log [BRANCH]` | List saved versions with readable dates, times, messages, and snapshot IDs. Defaults to the current branch. |
| `inspect` | Choose a changed path from the current saved version and view its patch in a read-only terminal viewer. |
| `revert [BRANCH] [SNAPSHOT]` | Restore a saved version and select its branch. Omit the snapshot ID to open the snapshot picker; the branch defaults to the current branch. |
| `finalize DESCRIPTION [TICKET]` | Save a normal commit in the ticket folder and export a named patch directly into the patch root. The ticket defaults to the current branch. |
| `pull` | Save pending changes for recovery, then run SVN update at the working-copy root, excluding externals. |

Examples:

```powershell
gitsvn switch
gitsvn revert 12345
gitsvn revert 12345 20261002_143000_123456
gitsvn finalize "Fix input validation" 12345
gitsvn finalize SavedVersion 12345 --latest
gitsvn pull
```

`gitsvn switch` opens a terminal menu of local branches, including `trunk`, with the current branch highlighted. Use **Up/Down** to choose and **Enter** to switch; **Esc** or **q** cancels. Long lists scroll as you move. When the terminal cannot display the menu or input/output is redirected, it shows a numbered list instead. Selecting the current branch or cancelling leaves your work and history untouched.

`gitsvn revert` uses the same controls to choose a snapshot from the current branch; `gitsvn revert 12345` chooses from that branch. Each entry shows its date, time, message, and snapshot ID. The branch's **saved head** is marked and selected initially; if none is recorded, the newest snapshot is selected. Cancelling leaves your work and history untouched. After restoring, the selected snapshot becomes the branch's head, even when the recovery autosave has a newer timestamp.

`log` shows local dates and times as `DD.MM.YYYY HH:MM:SS`. The snapshot ID appears in brackets after the message; use that exact ID with `revert`. The branch's saved head is marked **`(current version)`**, even when a newer recovery autosave appears above it. Dates come from generated snapshot names, or the file's last-modified time for older imported patches. Restoring an older version makes it the branch's saved head for future switches.

`gitsvn inspect` lists the changed paths in that **current version**, including properties and empty files or directories. It reads the saved head, so newer recovery autosaves and edits made since that commit do not appear. Use **Up/Down** and **Enter** to pick a path. The read-only viewer shows its patch hunks with their surrounding context; it does not load the entire file. Additions are green, deletions red, and hunk headers cyan. Use **Up/Down**, **PgUp/PgDn**, or **Home/End** to scroll, and **Left/Right** to view long lines. **Esc** or **q** returns to the path list; pressing it again there quits. In an unsupported terminal or with redirected input/output, a numbered picker prints the selected diff without colors and repeats until **q** or end of input. Inspection leaves your working copy and saved history untouched.

`status` uses the same change filter as branch snapshots and switching. It includes current tracked edits, including changes made since the last commit, and excludes unversioned files, SVN-ignored files, externals, and `ignore-on-commit` edits. Add new files with `gitsvn add` to include them. Rows use **A** for additions, **M** for text or property modifications, and **D** for deletions; conflicts and missing files remain visible. The status letters keep their colors: A is green, M yellow, and D red. Filenames are white when their changes are included in the saved head and red when their current changes differ from that head. Colors appear in supported terminals, while redirected output stays plain text.

Committing saves the current changes and turns their filenames white. Restoring an older snapshot compares against that selected head, even if a newer recovery autosave exists. With no recorded head, every visible filename is red; a missing or invalid recorded head reports a comparison error. Unsupported snapshot changes remain visible with red filenames. Reverting a path to SVN BASE removes its status row, even if the saved head changed that path. Status only reads the working copy and saved snapshots.

`finalize` normally captures current changes. With `--latest`, it uses the current branch's saved head, or the newest saved patch when no head is recorded. An explicit ticket selects the destination folder and keeps the current branch selected. Finalizing updates the destination's saved head.

Descriptions must start with a letter or number and use letters, numbers, spaces, dots, underscores, or hyphens. Spaces become underscores in the export filename. Existing finalized patches are never overwritten; choose another description for another export on the same day.

## Configuration

The default configuration is `.gitsvn\config.json` beside `main.py`. All settings are optional:

| Setting | Meaning |
| --- | --- |
| `svn_root` | SVN working-copy root. Configure this for your checkout. |
| `patch_root` | Folder for branch snapshots and finalized patches; defaults to `D:\Patches`. |
| `state_dir` | Folder for local branch state; defaults to the configuration file's directory. |
| `autosave` | Save outgoing changes when switching branches; defaults to `true`. |

Setting `state_dir` changes where state is stored; the configuration continues to load from the same file. Relative paths in the configuration are resolved from that file's directory. Relative command-line paths are resolved from your current directory.

Command-line options override the corresponding configuration settings and go **before the command**. For example, a different configuration can select another working copy without changing your usual setup:

```powershell
gitsvn --config C:\Work\another-project.json status
```

Without `--config`, an explicit `--state-dir PATH` loads `PATH\config.json` for compatibility with existing setups. When both are supplied, `--config` selects the file and `--state-dir` overrides where state is stored. `--svn-root` and `--patch-root` are also available as overrides. Missing optional configuration or omitted settings keep the existing defaults. A file explicitly selected with `--config` must exist.

`autosave` controls branch switching:

- **`true`:** switching saves outgoing changes as a commit before restoring the destination branch.
- **`false`:** switching creates no outgoing commit. Commit work you want to keep first; returning to the branch restores its saved head.

A failed switch restores outgoing changes from a recovery copy. With autosave disabled, that copy is temporary and adds no history entries. If rollback cannot finish, the recovery files remain and the error reports their location. Explicit `revert` and `pull` continue to save recovery snapshots regardless of this setting.

## Saved files

With the default patch root, files are organized as follows:

```text
D:\Patches\
    12345\
        20261002_143000_123456.patch
        20261002_143000_123456.txt
        20261002_143000_123456.json
    YYYY_MM_DD_FixInputValidation_<author-suffix>.patch
```

Keep the branch's `.patch`, `.txt`, and `.json` files together. Metadata preserves details such as empty files, directories, and empty-file properties. Existing patches without matching message files can also be listed and restored.

Finalizing saves the usual timestamped files inside the ticket folder. Directly in the patch root, it creates **only the finalized `.patch`**. Its filename uses today's date, the description, and an author suffix currently fixed in the implementation. The command prints the exact export path.

Patch headers are always relative to the selected SVN working-copy root, regardless of where you run the command. For example, a file at `D:\Source\ExampleProject\src\Example.cs` appears as `Index: src/Example.cs`.

## Supported changes

The snapshot workflow supports text changes, SVN properties, additions, deletions, and empty files or directories. Files in the `ignore-on-commit` changelist are excluded and preserved. Unversioned and SVN-ignored files stay untouched; schedule new files with `add` before committing them.

Binary changes, SVN copies or moves, replacements, missing files, and unresolved conflicts stop snapshot creation before changes are reverted. A scheduled deletion that still exists on disk needs its local data moved aside first. Resolve SVN update conflicts with your usual SVN tools before switching branches.

## Help and checks

```powershell
gitsvn --help
gitsvn finalize --help
```

To run the integration checks from the program folder, also make `svnadmin` available on PATH:

```powershell
python -m unittest discover -s tests -v
```

The checks use temporary local SVN repositories and separate state and patch folders.
