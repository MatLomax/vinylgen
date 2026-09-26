"""A named synthetic case: one scene plus the facts a bound needs about it."""

from __future__ import annotations

from dataclasses import dataclass, field

from vinylgen.fidelity.scene import Scene


@dataclass(frozen=True)
class Case:
    """``case_id`` is unique and stable (family, parameters and size, e.g.
    ``star-5p-r0.4-256``); it names the committed files of a fixed-set case.
    ``min_feature_px`` is the narrowest feature the scene draws in pixels (a stroke
    or ring width, a star tip's width at its base, a disc's diameter), recorded for
    reporting; the regression bounds apply the raster's resolution floors locally,
    to each colour's narrow parts (``bounds.narrow_parts``). ``tags`` group cases
    for reporting and tuning (family, sweep kind:
    ``scaled`` or ``fixed-px``)."""

    case_id: str
    scene: Scene
    min_feature_px: float
    tags: tuple[str, ...] = field(default_factory=tuple)
