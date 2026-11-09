# Changelog

All notable changes to **vinylgen** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **Saving in the Nodes stage keeps the editor as it is.** A save from nib's editor made a new
  trace revision and then reset the editor: fit zoom, the layers panel open again, and the
  selection, collapsed groups, layer visibility, lock and isolation and the undo history all
  gone. The new revision's map is the saved text byte for byte, so the editor now stays mounted
  through the save, the new revision's artefacts loading (or failing to load) and its props
  moving onto that map; the next save posts to the new revision. nib core 0.9.5's save point
  makes the editor read saved, so the trace-run switcher unlocks, and undo still undoes the edit.
