# Changelog

All notable changes to **vinylgen** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Save progress, a green save toast and a full-screen editor in the Nodes stage** (nib core
  0.11.0). While a save posts, nib's Save button is disabled and its icon turns, and a second
  save waits for the first. A save that makes a new trace revision says so in a green toast that
  clears itself after 4 s; a refusal or a failed post stays on the line in red, and the stage's
  own notices show as information. A "Full screen" button on nib's toolbar puts the editor (its
  canvas, toolbar, layers panel and tools) over the whole screen, through the browser's full
  screen where it has one and as a fixed layer over the page where it does not (iPhone Safari);
  the button or Esc leaves it, and the view keeps its centre and zoom.
- **The Nodes stage's editor is nib core 0.10.0.** It adds the Add node tool (the toolbar's
  "Add node": one tap on a boundary inserts one node where the boundary tool would snap, then the
  tool disarms) and the symmetric node in label maps: the type button cycles cusp, smooth and
  symmetric (a node with a straight side passes over symmetric), and a symmetric node is saved as
  the map's `node` letter `z`, which the save route accepts and t3 reads and writes back as `z`.

### Fixed

- **The Nodes stage's editor is nib core 0.11.1.** Its chrome now writes only the attributes that
  change: before, every frame rewrote the same `hidden`, `disabled`, `title` and `aria-*` values,
  which queued a DOM mutation and invalidated style once per frame while nothing changed.
- **Editing nodes in the Nodes stage** (nib core 0.10.0). A smooth node stays a tangent join
  through every edit; a double-click, or a double-tap on a touch screen, inserts the node at the
  exact nearest point of the segment, first time, with a touch reach sized to the finger; a path
  selected on the canvas scrolls its row in the paths panel into view; deleting a node keeps the
  shape; and the boundary tool keeps working after a pick, a merge, an undo or a redo changes the
  map under it. A new boundary is drawn two-tone (light under a dark dash), as are its node
  squares and the face hatch, so it shows on a black face.
- **GLOSSARY.md's smooth and symmetric nodes were swapped.** They now carry Inkscape's and nib's
  meaning: a smooth node (`s`, square) is a tangent join whose handles may differ in length, and
  a symmetric node (`z`, circle) has equal handles. `docs/svg-tracing-spec.md`'s node-type table
  follows.

- **Saving in the Nodes stage keeps the editor as it is.** A save from nib's editor made a new
  trace revision and then reset the editor: fit zoom, the layers panel open again, and the
  selection, collapsed groups, layer visibility, lock and isolation and the undo history all
  gone. The new revision's map is the saved text byte for byte, so the editor now stays mounted
  through the save, the new revision's artefacts loading (or failing to load) and its props
  moving onto that map; the next save posts to the new revision. nib core 0.9.5's save point
  makes the editor read saved, so the trace-run switcher unlocks, and undo still undoes the edit.
