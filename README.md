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

## Example

```bat
gitsvn branch 18814
gitsvn switch 18814
gitsvn add kingspan\path\new-file.cs
gitsvn commit -m "Fix ticket 18814"
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
| `switch NAME` | Save current changes automatically, then restore the selected branch's saved head. For an existing folder without local state, use its newest patch. |
| `commit -m MESSAGE` | Save the current branch's complete SVN BASE diff as a timestamped `.patch` and matching `.txt` message. |
| `log [BRANCH]` | List snapshot timestamps and messages for the selected or current branch. |
| `revert [BRANCH] [SNAPSHOT]` | Save current changes automatically, then restore a snapshot and select its branch. Without a snapshot, show an interactive timestamp/message picker. |
| `pull` | Run `svn update` in the SVN working copy. |
| `status` | Show the current local branch and SVN working-copy status. |
| `add PATH...` | Run `svn add` for paths relative to the SVN working copy. |

For example, `gitsvn revert 18814 20261002_143000_123456` restores that saved timestamp. Use the exact snapshot name shown by `log`. Restoring an older snapshot makes it the branch's saved head for future switches.

Every patch is a complete snapshot relative to SVN BASE, not an incremental commit. Switching or restoring first reverts the working copy and then applies one patch; snapshots must not be applied cumulatively. Automatic saves preserve the outgoing branch's changes before a switch or restore. A rejected patch causes restoration to roll back. Committing clean trunk state saves an empty patch, so returning to a clean branch is also remembered.

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
