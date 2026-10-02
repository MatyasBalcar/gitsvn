# gitsvn

A local, Git-like workflow for an SVN working copy. Requires Python 3.12 or newer and `svn` on PATH. Commands save patches; they do not commit to SVN or create branches on the SVN server.

Register the launcher in your user PATH once, then open a new terminal to run `gitsvn` from any directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Patches\GitSVN\install.ps1
```

The installer preserves existing PATH entries and does not add duplicates. You can also run `D:\Patches\GitSVN\gitsvn.cmd` directly.

Defaults:

- SVN working copy: `D:\Elektlabs`
- Patch folders: `D:\Patches\<branch>`
- Local state: `.gitsvn` beside `main.py`
- Initial branch: `trunk`

## Configuration

Settings live in `.gitsvn/config.json` beside `main.py`, or in the directory selected by `--state-dir`:

```json
{
  "autosave": true
}
```

`autosave` defaults to `true` when the file or setting is missing. Set it to `false` to stop `switch` from creating outgoing commits. Commit changes you want to keep before switching; returning to that branch restores its saved head. A failed switch still restores your outgoing changes using a temporary recovery copy, which creates no history entries. Explicit `revert` and `pull` continue to save recovery snapshots.

## Example

```bat
gitsvn branch 18814
gitsvn switch 18814
gitsvn add kingspan\path\new-file.cs
gitsvn commit -m "Fix ticket 18814"
gitsvn finalize FixSviewPriority
gitsvn log 18814
gitsvn switch trunk
gitsvn revert 18814
gitsvn pull
```

## Commands

| Command | Behavior |
| --- | --- |
| `branch` | List local branches. |
| `branch NAME` | Create the branch folder and initialize a clean-trunk snapshot when needed. Does not switch branches. |
| `switch NAME` | Restore the selected branch's saved head, saving outgoing changes when `autosave` is true. For an existing folder without local state, use its newest patch. |
| `commit -m MESSAGE` | Save the current branch's complete SVN BASE diff as a timestamped `.patch` and matching `.txt` message. |
| `finalize DESCRIPTION [TICKET]` | Save a normal timestamped commit under the ticket folder and export only `YYYY_MM_DD_DESCRIPTION_BalcarM.patch` directly under the patch root. The ticket defaults to the current branch. Add `--latest` to use the branch's saved snapshot instead of current changes. |
| `log [BRANCH]` | List snapshot timestamps and messages for the selected or current branch. |
| `revert [BRANCH] [SNAPSHOT]` | Save current changes automatically, then restore a snapshot and select its branch. Without a snapshot, show an interactive timestamp/message picker. |
| `pull` | Run `svn update` in the SVN working copy. |
| `status` | Show the current local branch and SVN working-copy status. |
| `add PATH...` | Run `svn add` for paths relative to the SVN working copy. |

For example, `gitsvn revert 18814 20261002_143000_123456` restores that saved timestamp. Use the exact snapshot name shown by `log`. Restoring an older snapshot makes it the branch's saved head for future switches.

`gitsvn finalize FixSviewPriority 18814` saves a normal timestamped `.patch`, `.txt` and `.json` commit under `D:\Patches\18814`, then exports the same patch content as `D:\Patches\YYYY_MM_DD_FixSviewPriority_BalcarM.patch` using today's date. The patch root receives only the finalized `.patch` file. Omit `18814` to use the current branch's folder. Quoted descriptions such as `"Fix Sview Priority"` use underscores in the filename. Existing finalized patches are never overwritten; use a different description for another export on the same day. Finalizing updates the destination ticket's saved head and keeps the active branch selected. `--latest` uses the source branch's saved head, including an older snapshot selected with `revert`, or the newest patch when there is no recorded head.

Every patch is a complete snapshot relative to SVN BASE, not an incremental commit. Switching or restoring first reverts the working copy and then applies one patch; snapshots must not be applied cumulatively. With `autosave` enabled, automatic saves preserve outgoing changes before a switch. Explicit restores also save outgoing changes. A rejected patch causes restoration to roll back. Committing clean trunk state saves an empty patch, so returning to a clean branch is also remembered.

Each snapshot has a matching `.txt` message and a small `.json` metadata file for empty files, directories and empty-file properties. Keep these files together. A new branch's initial patch is empty and its message says it was created from trunk; creating a branch leaves current edits in place until you switch.

SVN commands always run at `D:\Elektlabs`, regardless of the directory where you invoke `gitsvn`. Patch headers are relative to that root, for example `Index: masa/applications/www.test/UnitTests/SViewPriorityTest.cs`; they do not include a drive letter or an `Elektlabs/` prefix. Snapshot files are stored separately under `D:\Patches\<branch>`.

Files in SVN's `ignore-on-commit` changelist remain excluded from saved commits. Unversioned and SVN-ignored files stay untouched; unversioned files are not captured until added with `gitsvn add` or `svn add`. Existing `.patch` files are listed and usable even without a matching `.txt` message.

Binary changes, SVN copies/moves, replacements, missing files and unresolved conflicts cannot be captured safely by this patch workflow. A scheduled deletion that still exists on disk also needs its local data moved aside first. The command stops before reverting these changes. `pull` saves current changes before updating; resolve any update conflicts with your usual SVN tools before switching.

Override paths before the command:

```bat
gitsvn --svn-root C:\work\svn --patch-root C:\work\patches --state-dir C:\work\gitsvn-state status
```

Run `gitsvn --help` or `gitsvn COMMAND --help` for command options.

The integration checks use temporary local SVN repositories and never touch `D:\Elektlabs`:

```bat
python -m unittest discover -s tests -v
```
