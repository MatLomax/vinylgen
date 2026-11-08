from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vinylgen.contracts import upgrade_trace_record


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Project(ApiModel):
    id: str
    slug: str
    name: str
    created_at: datetime
    updated_at: datetime


class Revision(ApiModel):
    id: str
    project_id: str
    parent_revision_id: str | None
    kind: str
    message: str | None
    settings: dict[str, Any]
    created_at: datetime


class Artifact(ApiModel):
    id: str
    revision_id: str
    kind: str
    filename: str
    media_type: str
    byte_size: int
    sha256: str
    created_at: datetime
    download_url: str


class ProjectDetail(Project):
    revisions: list[Revision]


JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class Job(ApiModel):
    id: str
    project_id: str | None
    revision_id: str | None
    result_revision_id: str | None
    kind: str
    status: JobStatus
    progress: float = Field(ge=0, le=1)
    message: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CreateProject(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CreateRevision(BaseModel):
    parent_revision_id: str | None = None
    kind: str = Field(default="settings", min_length=1, max_length=60)
    message: str | None = Field(default=None, max_length=500)
    settings: dict[str, Any] = Field(default_factory=dict)


class PaletteJobSettings(BaseModel):
    initial_colours: int = Field(default=16, ge=2, le=64)
    final_colours: int | None = Field(default=None, ge=2, le=64)
    histogram_bits: int = Field(default=5, ge=1, le=8)
    analysis_max_dimension: int = Field(default=640, ge=32)
    dither_radius: int = Field(default=1, ge=0, le=5)
    similar_delta_e: float = Field(default=28.0, ge=0)
    small_share: float = Field(default=0.004, ge=0, le=1)
    small_delta_e: float = Field(default=36.0, ge=0)
    fold_small_colours: bool = True
    fragmented_share: float = Field(default=0.035, ge=0, le=1)
    fragmented_largest_region: float = Field(default=0.30, ge=0, le=1)
    fragmented_delta_e: float = Field(default=24.0, ge=0)
    merge_fragmented_colours: bool = True
    min_output_colours: int = Field(default=2, ge=2)
    min_region_pixels: int = Field(default=24, ge=0)
    min_feature_width: int = Field(default=0, ge=0)
    min_colour_share: float = Field(default=0.01, ge=0, le=1)
    intentional_line_min_contrast: float = Field(default=12.0, ge=0)
    intentional_line_min_length: int = Field(default=8, ge=1)
    protect_intentional_lines: bool = True
    aa_ambiguity_margin: float = Field(default=7.0, ge=0)
    aa_smoothness: float = Field(default=2.5, ge=0)
    aa_resolution_passes: int = Field(default=5, ge=0)
    resolve_antialiasing: bool = True
    flatten_upscale: int = Field(default=1, ge=1, le=4)
    flatten_smooth_edges: bool = True
    protected_component_points: tuple[tuple[int, int], ...] = ()
    preserved_colour_points: tuple[tuple[int, int], ...] = ()
    distinct_colour_point_pairs: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = ()

    @model_validator(mode="after")
    def final_count_fits_candidates(self) -> "PaletteJobSettings":
        if self.final_colours is not None and self.final_colours > self.initial_colours:
            raise ValueError("final_colours cannot exceed initial_colours")
        points = [*self.protected_component_points, *self.preserved_colour_points]
        points.extend(point for pair in self.distinct_colour_point_pairs for point in pair)
        if any(x < 0 or y < 0 for x, y in points):
            raise ValueError("palette selection coordinates must be non-negative")
        return self


class CleanupJobSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width_mm: float | None = Field(default=None, gt=0)
    height_mm: float | None = Field(default=None, gt=0)
    remove_colour: str | None = None
    cleanup: bool = False
    cleanup_action: Literal["remove", "widen", "remove_then_widen"] = "remove"
    min_detail_width: float = Field(default=0.0, ge=0)
    min_region_area: float = Field(default=0.0, ge=0)
    remove_below_width: float = Field(default=0.0, ge=0)


class TraceJobSettings(BaseModel):
    # The physical size, removed colour and cutter cleanup belong to the cleanup
    # stage; a trace request carrying them is rejected rather than silently traced
    # without them.
    model_config = ConfigDict(extra="forbid")

    # The soft node limits (contracts.TraceSettings), each only warning, and each curve
    # kind's tube half-width in pixels, which steers t3: > 0, or None (off) on arcs,
    # ellipses and cubics. The line is never off. Each arc kind's minimum sweep, in degrees,
    # in [0, 360] (0: no minimum), and 0 on a kind that is off.
    soft_path_nodes: int = Field(default=5_000, ge=1)
    soft_layer_nodes: int = Field(default=50_000, ge=1)
    soft_document_nodes: int = Field(default=200_000, ge=1)
    half_line: float = Field(default=1.75, gt=0)
    half_arc: float | None = Field(default=1.75, gt=0)
    half_ellipse: float | None = Field(default=1.75, gt=0)
    half_cubic: float | None = Field(default=1.75, gt=0)
    min_sweep_arc: float = Field(default=0.0, ge=0, le=360, allow_inf_nan=False)
    min_sweep_ellipse: float = Field(default=0.0, ge=0, le=360, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def upgraded_record(cls, values):
        """A request from an older client, still sending retired fields
        (``contracts.RETIRED_TRACE_FIELDS``: the old fitter's lengths, their unit,
        corner, node and snap controls, overlap, solid underlayers, layer order), is read
        without them."""
        return upgrade_trace_record(values) if isinstance(values, dict) else values

    @field_validator("min_sweep_arc", "min_sweep_ellipse", mode="before")
    @classmethod
    def minimum_is_a_number(cls, value):
        """A minimum sweep is a number of degrees: a bool is refused, not read as 0 or 1."""
        if isinstance(value, bool):
            raise ValueError("a minimum sweep is a number of degrees, not a bool")
        return value

    @model_validator(mode="after")
    def no_minimum_on_an_off_kind(self) -> "TraceJobSettings":
        """A minimum sweep above 0 on a kind that is off is refused: an off kind draws nothing
        to restrict, and t3api refuses the setting."""
        for kind in ("arc", "ellipse"):
            if getattr(self, f"half_{kind}") is None and getattr(self, f"min_sweep_{kind}") > 0:
                raise ValueError(f"min_sweep_{kind} is above 0 while {kind} is off")
        return self


class StartJob(BaseModel):
    kind: Literal["foundation-demo", "palette", "cleanup", "trace", "validate", "export"] = "foundation-demo"
    project_id: str | None = None
    revision_id: str | None = None
    steps: int = Field(default=20, ge=1, le=200)
    step_delay_ms: int = Field(default=100, ge=10, le=2000)
    palette: PaletteJobSettings | None = None
    cleanup: CleanupJobSettings | None = None
    # Re-run the last cleanup with its own recorded settings (pipeline.rerun_cleanup_settings)
    # instead of ``cleanup``; the server names its removed colour by hex.
    rerun_last_cleanup: bool = False
    trace: TraceJobSettings | None = None
    colour_names: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def rerun_is_a_settingless_cleanup(self) -> "StartJob":
        if self.rerun_last_cleanup and (self.kind != "cleanup" or self.cleanup is not None):
            raise ValueError("rerun_last_cleanup is a cleanup job without cleanup settings")
        return self


class GenerationSettings(BaseModel):
    width: int = Field(default=1536, ge=256, le=4096)
    height: int = Field(default=1024, ge=256, le=4096)
    desired_colours: int = Field(default=6, ge=2, le=12)
    required_motifs: list[str] = Field(default_factory=list, max_length=20)
    rejected_motifs: list[str] = Field(default_factory=list, max_length=20)


class UpscaleSettings(BaseModel):
    upscaler: str = "4xNomos8kSCHAT-S.pth"
    upscale_factor: float = Field(default=2.0, ge=1.0, le=4.0)
    checkpoint: str = "blue_pencil-XL-v7.0.0.safetensors"
    denoise: float = Field(default=0.7, ge=0.0, le=1.0)
    controlnet: bool = True
    controlnet_strength: float = Field(default=0.4, ge=0.0, le=2.0)
    sample_size: int = Field(default=1024, ge=512, le=1024)
    tile_size: int = 512
    seed: int | None = None


class SelectOption(BaseModel):
    value: str
    label: str


class UpscaleOptionsAvailable(BaseModel):
    """The installed models ComfyUI reports, with the tuned defaults and cap."""

    available: Literal[True] = True
    upscalers: list[SelectOption]
    checkpoints: list[SelectOption]
    controlnets: list[SelectOption]
    defaults: UpscaleSettings
    sample_size_max: int


class UpscaleOptionsUnavailable(BaseModel):
    """ComfyUI could not be reached, so no upscale settings can be offered."""

    available: Literal[False] = False


UpscaleOptions = Annotated[UpscaleOptionsAvailable | UpscaleOptionsUnavailable,
                           Field(discriminator="available")]


class StartUpscale(BaseModel):
    settings: UpscaleSettings = Field(default_factory=UpscaleSettings)


class StartGeneration(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    project_name: str | None = Field(default=None, max_length=120)
    provider: Literal["codex"] = "codex"
    settings: GenerationSettings = Field(default_factory=GenerationSettings)


class GenerationStarted(BaseModel):
    project: Project
    job: Job
    expanded_prompt: str


EditorTool = Literal["merge_colour", "rename_colour", "recolour_colour", "reassign_region", "paint_pixels"]

Channel = Annotated[int, Field(ge=0, le=255)]


class LabelEditOperation(BaseModel):
    tool: EditorTool
    label: int | None = Field(default=None, ge=0)
    source_label: int | None = Field(default=None, ge=0)
    target_label: int | None = Field(default=None, ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    rgb: tuple[Channel, Channel, Channel] | None = None
    point: tuple[int, int] | None = None
    pixels: tuple[tuple[int, int], ...] | None = Field(default=None, max_length=4_000_000)


class ApplyLabelEdits(BaseModel):
    operations: list[LabelEditOperation] = Field(min_length=1, max_length=500)
    message: str | None = Field(default=None, max_length=500)


class InheritedRemoveColour(BaseModel):
    # A value a Cleanup run resolves to the last cleanup's removed colour, or None with
    # ``unresolved`` when no single colour of the current palette is that colour.
    value: str | None
    unresolved: bool
    # The last cleanup's removed colour's hex, which the form shows while unresolved.
    hex: str | None = None
    # Why it is unresolved: "missing" (no colour has the hex) or "ambiguous".
    problem: Literal["missing", "ambiguous"] | None = None


class EditorState(BaseModel):
    width: int
    height: int
    palette: list[dict[str, Any]]
    protected_pixels: int
    # The palette index of the colour the active cleanup removes (editor.removed_colour_index).
    removed_colour_index: int | None = None
    # The removed colour the Cleanup form inherits (editor._inherited_remove_colour).
    inherited_remove_colour: InheritedRemoveColour | None = None


class TracerSetting(BaseModel):
    """t3api's canonical setting (``t3api.normal_setting``): each curve kind's tube
    half-width (``None`` off) and the limits A1, A2 and L."""

    half: dict[str, float | None]
    A1: float
    A2: float
    L: float


class TracerInfo(BaseModel):
    """The tracer a trace run uses: t3api, its build and its default setting."""

    name: str
    build: str
    setting: TracerSetting


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    max_upload_bytes: int = 100 * 1024 * 1024
    active_jobs: int = 0
    tracer: TracerInfo | None = None
