# Vendored packages

Packages the web client installs by path (`web/package.json`, `file:vendor/...`).

## nib-core-0.1.0.tgz

`@nib/core` 0.1.0, nib's framework-free editor core. The Trace stage's Nodes overlay mounts its `Editor` with
`mountEditor` on the shown revision's `map.json` (`web/src/NibEditor.tsx`), and a save posts the map nib wrote to
`POST /api/revisions/{revision_id}/map`, which makes a new trace revision.

| | |
|---|---|
| sha256 | `415efe32a2dc10090917172741fb3be9fb1d6df280c71ee561da192018b9b021` |
| version | `0.1.0` |
| packed from | nib (`/home/mat/projects/nib`) at tag `core-v0.1.0`, commit `21e34c1b10b1d62f06511bb4400795de816aa21b` |
| packed by | `pnpm pack --pack-destination <vinylgen>/web/vendor` in nib's `packages/core` (its `prepack` rebuilds `dist/`); the pack is reproducible |
| holds | `package/package.json`, `package/LICENSE`, `package/dist/` (ESM JavaScript, `.d.ts`, source maps) |

## Replacing it with a new pack

1. In nib, check out the tag to ship (`git -C <nib> describe --tags` names it) and run `pnpm pack
   --pack-destination <vinylgen>/web/vendor` in `packages/core`.
2. Delete the old tarball from this folder. If the version changed, change the `file:` path in
   `web/package.json` to the new file name.
3. Update this README: file name, sha256 (`sha256sum`), version, tag and commit.
4. Run `npm --prefix web install` so `web/package-lock.json` records the new tarball's integrity, then
   `just test` and `npm --prefix web run build`. `just deploy` runs `npm --prefix web ci`, which installs the
   tarball the lockfile records.
