"""A save from nib's editor: the planar-map file nib wrote for a shown trace revision.

The web client embeds nib's editor (``@nib/core``, vendored as ``web/vendor/nib-core-0.5.0.tgz``)
on a trace revision's ``map.json`` and posts the document nib wrote to
``POST /api/revisions/{revision_id}/map``. ``map_refusal`` decides whether that document may
become a new trace revision; ``PipelineRunner.save_map`` makes it one.
"""

from __future__ import annotations

# The editor a saved map was edited in, recorded in the new revision's ``trace.json``
# (``backend.editor``): the package the web client installs, ``web/vendor/README.md``.
NIB_EDITOR = {"name": "nib", "version": "0.5.0"}
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
    3. comparison: its ``W``, ``H``, ``palette`` and ``names`` are the parent's;
    4. unchanged: it is not byte-equal to the parent's ``map.json``.

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
    original = t3api.read_planar_map(parent.decode("utf-8"))
    problems = [f"{key} is {pm[key]!r}, the parent's is {original[key]!r}"
                for key in ("W", "H", "palette", "names") if pm[key] != original[key]]
    if problems:
        return _refusal(MISMATCH, "The map is not the size or palette of the revision it edits.",
                        problems)
    if body == parent:
        return _refusal(UNCHANGED, "Nothing was edited: the map is unchanged.", [])
    return None


def _refusal(reason: str, detail: str, problems: list[str]) -> dict:
    return {"detail": detail, "reason": reason, "problems": problems}
