# License for `chunky-mobs.patch`

`camera/chunky-mobs.patch` is a patch against [Chunky](https://github.com/chunky-dev/chunky),
which is licensed under the **GNU General Public License, version 3**. A patch is a
derivative work of the code it modifies, so **`chunky-mobs.patch` is GPLv3**, not MIT. The
MIT license in the repository root does not cover it.

Anything built from it is also GPLv3: that includes the `chunky-core` jar that
`build-chunky.sh` produces (in `camera/dist/`, git-ignored) and the Docker image built from
that jar. Neither is distributed in this repository.

## Which upstream commit it applies to

| | |
|---|---|
| upstream | `https://github.com/chunky-dev/chunky` |
| commit | `527cb4a26418515e1babe2f217be4813c42a42ca` |
| nightly it corresponds to | `2.5.0-SNAPSHOT.478.g527cb4a` |
| applied by | `camera/build-chunky.sh` (`git clone`, `git checkout <commit>`, `git apply`) |

`build-chunky.sh` clones that commit and applies the patch, so no Chunky source is
vendored here. Override the commit and version with `CHUNKY_COMMIT` and `CHUNKY_VERSION`.

## What the patch changes

It adds entity models so that living entities saved in `entities/*.mca` (endermen, zombies,
villagers) are drawn in the render. Upstream Chunky draws the blocks but not those mobs.
See `camera/RESEARCH-mobs.md` for the detail.

The full GPLv3 text is at <https://www.gnu.org/licenses/gpl-3.0.txt>.
