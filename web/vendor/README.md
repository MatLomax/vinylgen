# Vendored packages

Packages the web client installs by path (`web/package.json`, `file:vendor/...`).

## nib-core-0.7.0.tgz

`@nib/core` 0.7.0, nib's framework-free editor core and its chrome. The Nodes stage (`web/src/NibEditor.tsx`)
mounts its `Editor` with `mountEditor` on the selected revision's `map.json` and nib's chrome over it with
`mountChrome(container, editor, { actions: ['save'] })`, styled by `@nib/core/chrome.css`; the chrome's Save posts
the map nib wrote to `POST /api/revisions/{revision_id}/map`, which makes a new trace revision. The package depends on
`lucide` (the chrome's icon data), which `npm` installs with it. It reads and writes the planar map's setting in either form t3api writes: the per-kind half-width table (`{half, A1, A2, L}`, t3api 0.27.0) and the older `{w, A1, A2, L}` (up to 0.26.0), each written back byte for byte, so the Nodes stage opens maps traced with either. It has Retract handles (Shift+H, the chrome button "Retract handles (Shift+H)", Ctrl+click on one handle), a tap on a face that makes its label's layer active and marks its row in the layers panel, and draws a face whose ring is one piece closing on itself (with the hole it cuts in the face around it). 0.7.0 adds: hiding a face gives its area to its visible neighbour with the longest shared boundary (the taker's fill covers it, so a hidden face leaves no hole); Merge face (Delete on a face row, or Del with every node of one face selected) merges a face into that neighbour as one undoable edit; the layers panel's hide, lock and active buttons are lucide icons; the panel's delete button on a label map merges a face row and is disabled, with a title saying why, on a layer row; and a closed ring of one node takes an inserted node and a reshaped segment.

| | |
|---|---|
| sha256 | `bc5ed4daf9469c6221d3a9c0d4371a05978369bde381fd1750cd0b2c6a4b46aa` |
| version | `0.7.0` |
| packed from | nib (`/home/mat/projects/nib`) at tag `core-v0.7.0`, commit `766e2c2fdaa2109a18f282c05d1ea031acf4a136` |
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
