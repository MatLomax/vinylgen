"""A save from nib's editor: the planar-map file nib wrote for a shown trace revision.

The web client embeds nib's editor (``@nib/core``, vendored as ``web/vendor/nib-core-0.11.1.tgz``)
on a trace revision's ``map.json`` and posts the document nib wrote to
``POST /api/revisions/{revision_id}/map``. ``map_refusal`` decides whether that document may
become a new trace revision; ``PipelineRunner.save_map`` makes it one.
"""

from __future__ import annotations

# The editor a saved map was edited in, recorded in the new revision's ``trace.json``
# (``backend.editor``): the package the web client installs, ``web/vendor/README.md``.
NIB_EDITOR = {"name": "nib", "version": "0.11.1"}
# The message of the trace revision a save makes.
EDITED_MESSAGE = "edited in nib"

# What a document is refused for (the 422 body's ``reason``).
INVALID, MISMATCH, UNCHANGED = "invalid", "mismatch", "unchanged"
# A malformed document makes t3api's reader or check raise one of these.
_READ_ERRORS = (ValueError, KeyError, TypeError, IndexError, AttributeError)


class MapRefused(Exception):
    """A posted map the server will not save; ``body`` is the 422 response's JSON."""

    def __init__(self, body: dict):
        super().__init__(body["detail"])
        self.body = body


def map_refusal(body: bytes, parent: bytes) -> dict | None:
    """Why the posted planar-map file ``body`` cannot become a child of the trace revision whose
    ``map.json`` is ``parent``, or ``None`` when it can.

    The checks run in order, and the first that fails answers:
    1. read: ``body`` is UTF-8 text that ``t3api.read_planar_map`` accepts;
    2. check: ``t3api.planar_map_check`` finds no violation;
    3. the rule of the map: no edge has one label on both sides (``rule_problems``);
    4. comparison: its ``W`` and ``H`` are the parent's, and its ``palette`` and ``names`` are
       the parent's with entries only appended (``palette_problems``);
    5. labels: every face's label is an entry of its palette;
    6. unchanged: it is not byte-equal to the parent's ``map.json``.

    Returns ``{"detail", "reason", "problems"}`` (``reason`` ``"invalid"``, ``"mismatch"`` or
    ``"unchanged"``), with ``violations`` when the check refused it."""
    import t3api

    try:
        text = body.decode("utf-8")
        pm = t3api.read_planar_map(text)
    except UnicodeDecodeError as error:
        return _refusal(INVALID, "The map is not UTF-8 text.", [str(error)])
    except _READ_ERRORS as error:
        return _refusal(INVALID, "The map is not a planar-map file t3 can read.",
                        [str(error) or type(error).__name__])
    try:
        check = t3api.planar_map_check(pm)
    except _READ_ERRORS as error:
        return _refusal(INVALID, "The map is not a planar-map file t3 can check.",
                        [str(error) or type(error).__name__])
    if check["violations"]:
        refusal = _refusal(INVALID, f"The map fails the planar-map check with "
                                    f"{check['violations']} violation(s).",
                           [str(problem) for problem in check["problems"]])
        refusal["violations"] = int(check["violations"])
        return refusal
    rule = rule_problems(pm)
    if rule:
        return _refusal(INVALID, f"The map breaks the rule of the map: {rule[0]}.", rule)
    original = t3api.read_planar_map(parent.decode("utf-8"))
    problems = [f"{key} is {pm[key]!r}, the parent's is {original[key]!r}"
                for key in ("W", "H") if pm[key] != original[key]]
    if problems:
        return _refusal(MISMATCH, "The map is not the size of the revision it edits.", problems)
    problems = palette_problems(pm, original)
    if problems:
        return _refusal(MISMATCH, f"The map's palette is not the palette of the revision it "
                                  f"edits with colours only appended: {problems[0]}.", problems)
    problems = label_problems(pm)
    if problems:
        return _refusal(INVALID, "A face of the map is labelled with no palette entry.", problems)
    if body == parent:
        return _refusal(UNCHANGED, "Nothing was edited: the map is unchanged.", [])
    return None


def rule_problems(pm: dict) -> list[str]:
    """The rule of the map, over the map's own edge and face records: one problem per edge whose
    two sides are two faces of one label, naming the edge. Two adjacent regions of one label are
    one region, so such an edge is no boundary of the labels. An edge with a null side (a border
    edge) is exempt. The same rule as nib's ``checkLabelMap`` (``ruleProblems``)."""
    label = {face["face"]: face["label"] for face in pm["faces"]}
    problems = []
    for k, edge in enumerate(pm["edges"]):
        left, right = edge["left"], edge["right"]
        if left is None or right is None or left == right:
            continue
        if left in label and label[left] == label.get(right):
            problems.append(f"edge {k} has label {label[left]} on both sides "
                            f"(faces {left} and {right})")
    return problems


def is_colour(entry) -> bool:
    """Whether ``entry`` is a colour in a planar map palette's format: ``[r, g, b]``, three
    integers from 0 to 255."""
    return (isinstance(entry, list) and len(entry) == 3
            and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255
                    for v in entry))


def palette_problems(pm: dict, parent: dict) -> list[str]:
    """Why ``pm``'s ``palette`` and ``names`` are not ``parent``'s with entries only appended,
    first differing entry first; empty when they are.

    Every parent entry is kept at its index (none changed, reordered or removed); every appended
    palette entry is a colour (``is_colour``) equal to no earlier entry (a palette never holds one
    colour twice: two layers of one fill are two sheets of one vinyl colour) and labelling at
    least one face of ``pm``; the names keep the parent's the same way (null names stay null),
    and an appended name is null or a name no other entry holds, ignoring case, as each palette
    name is unique."""
    palette, original = pm["palette"], parent["palette"]
    problems = []
    for index, entry in enumerate(original):
        if index >= len(palette):
            problems.append(f"palette entry {index} {entry!r} is removed")
            break
        if palette[index] != entry:
            problems.append(f"palette entry {index} is {palette[index]!r}, "
                            f"the parent's is {entry!r}")
            break
    labels = {face["label"] for face in pm["faces"]}
    for index in range(len(original), len(palette)):
        earlier = next((j for j in range(index) if palette[j] == palette[index]), None)
        if not is_colour(palette[index]):
            problems.append(f"palette entry {index} {palette[index]!r} is not a colour [r, g, b] "
                            "of integers from 0 to 255")
        elif earlier is not None:
            problems.append(f"palette entry {index} {palette[index]!r} equals palette entry "
                            f"{earlier}")
        elif index not in labels:
            problems.append(f"palette entry {index} {palette[index]!r} labels no face")
    names, original_names = pm.get("names"), parent.get("names")
    if original_names is None or names is None:
        if names != original_names:
            problems.append(f"names is {names!r}, the parent's is {original_names!r}")
        return problems
    for index, name in enumerate(original_names):
        if index < len(names) and names[index] != name:
            problems.append(f"names entry {index} is {names[index]!r}, the parent's is {name!r}")
            break
    held = [str(name).casefold() for name in names if name is not None]
    for index in range(len(original_names), len(names)):
        name = names[index]
        if name is not None and held.count(name.casefold()) > 1:
            problems.append(f"names entry {index} {name!r} is another entry's name")
    return problems


def label_problems(pm: dict) -> list[str]:
    """One problem per face whose label is no index of ``pm``'s palette."""
    return [f"face {face['face']} has label {face['label']}, no entry of the "
            f"{len(pm['palette'])}-entry palette"
            for face in pm["faces"]
            if not (isinstance(face["label"], int) and 0 <= face["label"] < len(pm["palette"]))]


def _refusal(reason: str, detail: str, problems: list[str]) -> dict:
    return {"detail": detail, "reason": reason, "problems": problems}
