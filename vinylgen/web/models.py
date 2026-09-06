from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    min_colour_share: float = Field(default=0.01, ge=0, le=1)
    intentional_line_min_contrast: float = Field(default=12.0, ge=0)
    intentional_line_min_length: int = Field(default=8, ge=1)
    protect_intentional_lines: bool = True
    aa_ambiguity_margin: float = Field(default=7.0, ge=0)
    aa_smoothness: float = Field(default=2.5, ge=0)
    aa_resolution_passes: int = Field(default=5, ge=0)
    resolve_antialiasing: bool = True
    flatten_upscale: int = Field(default=2, ge=1, le=4)
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


class TraceJobSettings(BaseModel):
    width_mm: float | None = Field(default=None, gt=0)
    height_mm: float | None = Field(default=None, gt=0)
    overlap: float = Field(default=0.0, ge=0)
    solid_underlayers: bool = False
    remove_colour: str | None = None
    layer_order: list[str] | None = None
    corner_threshold: float = Field(default=57.3, ge=0, le=180)
    curve_optimization: bool = True
    curve_fit_tolerance: float = Field(default=0.8, gt=0)
    straight_line_max_deviation: float = Field(default=0.8, ge=0)
    minimum_straight_length: float = Field(default=4.0, ge=0)
    max_deviation: float | None = Field(default=None, gt=0)
    corner_analysis_span: float = Field(default=8.0, gt=0)
    cleanup: bool = False
    cleanup_action: Literal["remove", "widen", "remove_then_widen"] = "remove"
    min_detail_width: float = Field(default=0.0, ge=0)
    min_region_area: float = Field(default=0.0, ge=0)
    remove_below_width: float = Field(default=0.0, ge=0)
    soft_path_nodes: int = Field(default=5_000, ge=1)
    soft_layer_nodes: int = Field(default=50_000, ge=1)
    soft_document_nodes: int = Field(default=200_000, ge=1)


class StartJob(BaseModel):
    kind: Literal["foundation-demo", "palette", "trace", "validate", "export"] = "foundation-demo"
    project_id: str | None = None
    revision_id: str | None = None
    steps: int = Field(default=20, ge=1, le=200)
    step_delay_ms: int = Field(default=100, ge=10, le=2000)
    palette: PaletteJobSettings | None = None
    trace: TraceJobSettings | None = None
    colour_names: dict[str, str] = Field(default_factory=dict)


class GenerationSettings(BaseModel):
    width: int = Field(default=1536, ge=256, le=4096)
    height: int = Field(default=1024, ge=256, le=4096)
    desired_colours: int = Field(default=6, ge=2, le=12)
    required_motifs: list[str] = Field(default_factory=list, max_length=20)
    rejected_motifs: list[str] = Field(default_factory=list, max_length=20)


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


class EditorState(BaseModel):
    width: int
    height: int
    palette: list[dict[str, Any]]
    protected_pixels: int


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    max_upload_bytes: int = 100 * 1024 * 1024
