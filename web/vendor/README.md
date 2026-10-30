# Vendored packages

Packages the web client installs by path (`web/package.json`, `file:vendor/...`).

## nib-core-0.3.0.tgz

`@nib/core` 0.3.0, nib's framework-free editor core and its chrome. The Nodes stage (`web/src/NibEditor.tsx`)
mounts its `Editor` with `mountEditor` on the selected revision's `map.json` and nib's chrome over it with
`mountChrome(container, editor, { actions: ['save'] })`, styled by `@nib/core/chrome.css`; the chrome's Save posts
the map nib wrote to `POST /api/revisions/{revision_id}/map`, which makes a new trace revision. The package depends on
`lucide` (the chrome's icon data), which `npm` installs with it. 0.3.0 adds the node-type button showing the selected nodes' type and an Add handle action that grows a missing handle.

| | |
|---|---|
| sha256 | `aaa2abc4167f531ab8d1b453c0815aa5ff30e769dc6e3f5852c4a15565511dc8` |
| version | `0.3.0` |
| packed from | nib (`/home/mat/projects/nib`) at tag `core-v0.3.0`, commit `bdbb4bd212574404ff2b0c9ff0d82d741ed474a0` |
| packed by | `pnpm pack --pack-destination <vinylgen>/web/vendor` in nib's `packages/core` (its `prepack` rebuilds `dist/`); the pack is reproducible |
| holds | `package/package.json`, `package/LICENSE`, `package/dist/` (ESM JavaScript, `.d.ts`, source maps, `chrome.css`) |

## Replacing it with a new pack

1. In nib, check out the tag to ship (`git -C <nib> describe --tags` names it) and run `pnpm pack
   --pack-destination <vinylgen>/web/vendor` in `packages/core`.
2. Delete the old tarball from this folder. If the version changed, change the `file:` path in
   `web/package.json` to the new file name.
3. Update this README: file name, sha256 (`sha256sum`), version, tag and commit.
4. Run `npm --prefix web install` so `web/package-lock.json` records the new tarball's integrity, then
   `just test` and `npm --prefix web run build`. `just deploy` runs `npm --prefix web ci`, which installs the
   tarball the lockfile records.
