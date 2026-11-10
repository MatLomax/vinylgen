# Changelog

All notable changes to **vinylgen** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Bend any edge in the Nodes stage by dragging it, as in Inkscape** (nib core 0.12.0). A
  drag on any segment of the active face bends it, with nothing selected first: its two
  nodes become the selection, an arc converts to curves and bends in the same undo step,
  both faces of the edge follow, and one undo puts back the edge and the selection. A drag
  on another face's edge still pans, and a tap there makes that face active.
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

- **A touch double-tap in the Nodes stage inserts its node where the first tap landed, and a
  press near the active face pans again** (nib core 0.12.1). The second tap counts within the
  touch reach of the first tap's point and half a second, so jitter across a node, a first tap
  that made the face on its side active, or a selected node's handle under the second tap no
  longer loses the insert; the node goes on the boundary nearest the finger, whichever face
  is active. A drag bends a boundary only when it starts within 8 CSS px of it; further out it
  pans, as before 0.12.0. A press or release on a large map no longer stalls the screen: it
  redraws only the part of the map the edited faces cover, and the map's static picture is
  held in tiles bounded to what is on screen plus a ring (about 27 MB on a phone).

- **Drags and pans on a large map in the Nodes stage stay near a frame** (nib core 0.11.2). On a
  map with a background face of hundreds of rings (the fox, 458 rings and 17,054 nodes), a node or
  handle drag cost about 150 ms a move and a pan about 125 ms on a phone-speed CPU; they now cost
  about 25 to 30 ms. A face of many rings builds its path in linear time, each edge's path is kept
  until that edge changes, a pan or pinch reuses the static layer (rastered with a margin of one
  view) until the gesture ends, and what lies outside the view is not drawn.
- **The Nodes stage's chrome writes only the attributes that change** (nib core 0.11.1): before, every frame rewrote the same `hidden`, `disabled`, `title` and `aria-*` values,
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
