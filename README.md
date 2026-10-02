# gitsvn

A local, Git-like workflow for an SVN working copy. Save changes as patches, organize them into local branches, switch between saved versions, and export a final patch for a ticket.

Local commits are snapshots of your uncommitted SVN changes. Publishing changes to the SVN server remains a separate step using your usual SVN client.

## Setup

### Requirements

- Windows with Python 3.12 or newer.
- `python` and `svn` available on PATH.
- An existing SVN working copy.

### Choose the folders

The program can live on any drive, for example `C:\Tools\gitsvn`. Keep `main.py`, `gitsvn.cmd`, and `install.ps1` together. Choose a writable folder, or specify a separate writable state folder with `--state-dir`.

Patch storage defaults to **`D:\Patches`**. Your PC needs a `D:` drive to use that default. The first save creates the folder and its branch subfolders automatically; you can also create it yourself:

```powershell
New-Item -ItemType Directory -Force -Path D:\Patches
```

The program does not have to live inside the patch folder. If you do not have a `D:` drive, or prefer another location, pass `--patch-root C:\Patches`.

Both patch storage and local state must be **outside the SVN working copy**. By default, state and configuration live in `.gitsvn` beside `main.py`. If you keep the program inside an SVN checkout, select an external state folder with `--state-dir`.

### Make the command available everywhere

From the folder containing the program, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Open a new terminal afterward. The installer adds the program's actual location to your user PATH, so `gitsvn` works from any directory. If you move the program later, update its PATH entry. You can also run `gitsvn.cmd` directly by its full path.

### Select your working copy

Use `--svn-root` to point at the SVN working-copy root, the directory containing `.svn`. Supply your own path explicitly rather than relying on the default from the original installation.

For example, these PowerShell options select a checkout and a separate state folder:

```powershell
$gitsvnOptions = @(
    '--svn-root', 'D:\Source\ExampleProject',
    '--patch-root', 'D:\Patches',
    '--state-dir', 'C:\Work\gitsvn-state'
)

gitsvn @gitsvnOptions status
```

Options go **before the command** and must be supplied on each invocation. Use a separate state folder for each working copy; state records the selected checkout and patch root and refuses mismatched paths.

## Everyday workflow

Using the options above:

```powershell
gitsvn @gitsvnOptions branch 12345
gitsvn @gitsvnOptions switch 12345

# Edit files in the SVN working copy. Add new files before saving them.
gitsvn @gitsvnOptions add src\Example.cs
gitsvn @gitsvnOptions commit -m "Fix input validation"

gitsvn @gitsvnOptions log
gitsvn @gitsvnOptions finalize FixInputValidation
gitsvn @gitsvnOptions switch trunk
```

Creating a branch saves an empty initial snapshot of SVN BASE and leaves your current edits in place. `switch` restores that branch's saved version. Each commit contains the complete diff against SVN BASE, so restoring a version applies one snapshot rather than a sequence of commits.

## Commands

Use `gitsvn [options] <command>`:

| Command | What it does |
| --- | --- |
| `branch` | List local branches; `*` marks the current branch. |
| `branch NAME` | Create or locate a branch folder and initialize it when needed. |
| `switch NAME` | Restore the branch's saved head, optionally saving outgoing changes according to `autosave`. |
| `status` | Show the current branch and SVN status. |
| `add PATH...` | Schedule new files or folders with SVN. Paths are relative to the working-copy root. |
| `commit -m MESSAGE` | Save a timestamped patch, message, and snapshot metadata in the current branch folder. |
| `log [BRANCH]` | List saved versions and their messages. Defaults to the current branch. |
| `revert [BRANCH] [SNAPSHOT]` | Restore a saved version and select its branch. Without a snapshot name, show a numbered picker; enter `q` to cancel. |
| `finalize DESCRIPTION [TICKET]` | Save a normal commit in the ticket folder and export a named patch directly into the patch root. The ticket defaults to the current branch. |
| `pull` | Save pending changes for recovery, then run SVN update at the working-copy root, excluding externals. |

Examples:

```powershell
gitsvn @gitsvnOptions revert 12345
gitsvn @gitsvnOptions revert 12345 20261002_143000_123456
gitsvn @gitsvnOptions finalize "Fix input validation" 12345
gitsvn @gitsvnOptions finalize SavedVersion 12345 --latest
gitsvn @gitsvnOptions pull
```

Use the exact snapshot name shown by `log`. Restoring an older version makes it the branch's saved head for future switches.

`finalize` normally captures current changes. With `--latest`, it uses the current branch's saved head, or the newest saved patch when no head is recorded. An explicit ticket selects the destination folder and keeps the current branch selected. Finalizing updates the destination's saved head.

Descriptions must start with a letter or number and use letters, numbers, spaces, dots, underscores, or hyphens. Spaces become underscores in the export filename. Existing finalized patches are never overwritten; choose another description for another export on the same day.

## Configuration

Edit `config.json` in the selected state folder. With the default state location, this is `.gitsvn\config.json` beside `main.py`.

```json
{
  "autosave": true
}
```

`autosave` defaults to `true` when the file or setting is missing. Existing configuration takes precedence.

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
