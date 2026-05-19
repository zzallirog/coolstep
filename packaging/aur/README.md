# coolstep — AUR packaging

Two flavors, mirroring AUR convention:

| Package | Source | When to use |
|---|---|---|
| `coolstep-git` | git+master | Tracking the bleeding edge / contributing |
| `coolstep`     | tagged release tarball | Stable users, after first `vX.Y.Z` tag is cut |

## Building locally (smoke-test before pushing to AUR)

```bash
cd packaging/aur/coolstep-git
makepkg --check --noconfirm
namcap PKGBUILD coolstep-git-*.pkg.tar.zst   # static lint
```

The `check()` step runs the bundled pytest suite (excluding the chroma-bound
`test_daemon.py`).  Daemon test exclusion mirrors `COOLSTEP_CHROMA_DISABLED=1`
posture documented in master CLAUDE.md.

## Generating .SRCINFO

AUR requires .SRCINFO alongside PKGBUILD:

```bash
makepkg --printsrcinfo > .SRCINFO
```

(Don't hand-edit — regenerate every PKGBUILD bump.)

## Pushing to AUR

```bash
# First time:
git clone ssh://aur@aur.archlinux.org/coolstep-git.git aur-coolstep-git
cd aur-coolstep-git
cp ../packaging/aur/coolstep-git/PKGBUILD .
cp ../packaging/aur/coolstep-git/.SRCINFO .
git add PKGBUILD .SRCINFO
git commit -m "Initial import — coolstep-git 0.0.1"
git push origin master
```

Subsequent bumps: edit PKGBUILD, regenerate .SRCINFO, commit, push.

## When to cut a stable `coolstep` release

Recommended gate (per project policy):
1. ≥ 600 tests pass on master (current: 628/628)
2. Calibration window completed once on at least one reference machine
3. CHANGELOG.md updated with breaking changes flagged
4. `git tag v0.X.Y -a -m "Release v0.X.Y"` and push to GitHub
5. Update stable `coolstep` PKGBUILD with new `pkgver=` + actual sha256 of the tarball.

Until then, only `coolstep-git` is real — `coolstep` PKGBUILD is a stub
template for the first release.
