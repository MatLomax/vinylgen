from __future__ import annotations

import json
import mimetypes
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import WebConfig
from .comfy_client import COMFY_NOT_RUNNING, ComfyClient, ComfyUIError, ComfyUnavailable
from .jobs import JobManager
from .inkscape import InkscapeHandoff
from .generation import CodexImageProvider, ImageProvider, expand_prompt
from .editor import apply_edits, automatic_layer_order, editor_state
from .models import (ApplyLabelEdits, Artifact, AutomaticLayerOrder, CleanupJobSettings,
                     CreateProject, CreateRevision, EditorState, GenerationStarted, Health, Job,
                     Project, ProjectDetail, Revision,
                     StartGeneration, StartJob, StartUpscale, UpscaleOptions,
                     UpscaleSettings)
from .pipeline import rerun_cleanup_settings, served_revisions
from .store import Store
from .trace_preview import TracePreviewService, trace_preview_router
from .upscale import (DEFAULT_CONTROLNET_MODEL, SAMPLE_SIZE_MAX, UPSCALER_LANCZOS,
                     UPSCALER_NONE, TiledRegenProvider, UpscaleProvider)


def _comfy_options(client: ComfyClient) -> dict[str, list[str]]:
    """The installed upscale models, checkpoints and controlnets ComfyUI reports.

    A single ``/object_info`` enumeration shared by the options endpoint and the
    up-front upscale validation, so both read the same live source of truth.
    """
    return {
        "upscalers": client.object_info_options("UpscaleModelLoader", "model_name"),
        "checkpoints": client.object_info_options("CheckpointLoaderSimple", "ckpt_name"),
        "controlnets": client.object_info_options("ControlNetLoader", "control_net_name"),
    }


def _validate_upscale_settings(settings: UpscaleSettings,
                               options: dict[str, list[str]]) -> str | None:
    """The reason ``settings`` name a model ComfyUI does not have, or ``None``.

    ``upscaler`` must be ``none``, ``lanczos`` or a listed upscale model;
    ``checkpoint`` must be a listed checkpoint; and when ``controlnet`` is on the
    fixed tile-ControlNet model the graph wires must be a listed ControlNet.
    """
    if (settings.upscaler not in (UPSCALER_NONE, UPSCALER_LANCZOS)
            and settings.upscaler not in options["upscalers"]):
        return (f"Unknown upscaler {settings.upscaler!r}; choose "
                f"{UPSCALER_NONE!r}, {UPSCALER_LANCZOS!r}, or an installed "
                "ComfyUI upscale model")
    if settings.checkpoint not in options["checkpoints"]:
        return (f"Unknown checkpoint {settings.checkpoint!r}; choose an installed "
                "ComfyUI checkpoint")
    if settings.controlnet and DEFAULT_CONTROLNET_MODEL not in options["controlnets"]:
        return (f"Tile ControlNet model {DEFAULT_CONTROLNET_MODEL!r} is not "
                "installed in ComfyUI; install it or turn ControlNet off")
    return None


def create_app(config: WebConfig | None = None,
               generation_providers: dict[str, ImageProvider] | None = None,
               *,
               upscale_provider: UpscaleProvider | None = None,
               comfy_client_factory: Callable[[], ComfyClient] | None = None) -> FastAPI:
    config = config or WebConfig.from_env()
    store = Store(config)
    providers = generation_providers if generation_providers is not None else {
        "codex": CodexImageProvider()}
    jobs = JobManager(store, providers, upscale_provider or TiledRegenProvider())
    comfy_client_factory = comfy_client_factory or (lambda: ComfyClient())
    inkscape = InkscapeHandoff(store)
    trace_previews = TracePreviewService(store)
    admission_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Compile the tracer's kernels on the preview thread so the first live
        # preview is warm; startup and health do not wait for it.
        trace_previews.warm_up()
        yield
        trace_previews.shutdown()
        await jobs.shutdown()

    app = FastAPI(title="Vinylgen", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.jobs = jobs
    app.state.trace_previews = trace_previews
    app.include_router(trace_preview_router(trace_previews))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def protect_mutations(request: Request, call_next):
        if (config.require_proxy_identity and request.method not in {"GET", "HEAD", "OPTIONS"}
                and not request.headers.get("X-Gandalf-User")):
            return JSONResponse({"detail": "Authenticated Gandalf identity required"}, status_code=403)
        return await call_next(request)

    def missing(exc: KeyError) -> HTTPException:
        return HTTPException(404, f"Resource not found: {exc.args[0]}")

    def artifact(row: dict) -> Artifact:
        return Artifact(**row, download_url=f"/api/artifacts/{row['id']}/content")

    async def admit_job(*, kind: str, project_id: str | None,
                        revision_id: str | None) -> dict:
        # All HTTP admission runs on this event loop; the lock makes the count
        # and insert one indivisible capacity decision.
        async with admission_lock:
            if store.active_job_count() >= config.max_active_jobs:
                raise HTTPException(429, "Too many active jobs; try again after one finishes")
            return store.create_job(kind=kind, project_id=project_id,
                                    revision_id=revision_id)

    @app.get("/api/health", response_model=Health)
    async def health() -> Health:
        return Health(max_upload_bytes=config.max_upload_bytes,
                      active_jobs=store.active_job_count())

    @app.get("/api/projects", response_model=list[Project])
    async def list_projects() -> list[dict]:
        return store.list_projects()

    @app.post("/api/projects", response_model=Project, status_code=201)
    async def create_project(payload: CreateProject) -> dict:
        return store.create_project(payload.name)

    @app.post("/api/projects/import", response_model=ProjectDetail, status_code=201)
    async def import_project(name: str = Form(...), source: UploadFile = File(...)) -> dict:
        if not source.filename:
            raise HTTPException(400, "A source filename is required")
        if source.size is not None and source.size > config.max_upload_bytes:
            raise HTTPException(413, "Upload exceeds the configured limit")
        project, revision = store.import_project(
            name, filename=source.filename,
            media_type=source.content_type or "application/octet-stream", source=source.file,
        )
        return {**project, "revisions": [revision]}

    @app.post("/api/generations", response_model=GenerationStarted, status_code=202)
    async def start_generation(payload: StartGeneration) -> dict:
        async with admission_lock:
            if store.active_job_count() >= config.max_active_jobs:
                raise HTTPException(429, "Too many active jobs; try again after one finishes")
            project = store.create_project(payload.project_name or payload.prompt[:80])
            settings = {**payload.settings.model_dump(), "provider": payload.provider}
            job = store.create_job(kind="generate", project_id=project["id"], revision_id=None)
        jobs.start_generation(job["id"], project_id=project["id"], prompt=payload.prompt,
                              settings=settings)
        return {"project": project, "job": job,
                "expanded_prompt": expand_prompt(payload.prompt, settings)}

    @app.post("/api/projects/{project_id}/revisions/{revision_id}/upscale", status_code=202)
    async def start_upscale(project_id: str, revision_id: str, payload: StartUpscale) -> dict:
        try:
            revision = store.get_revision(revision_id)
        except KeyError as exc:
            raise missing(exc) from exc
        if revision["project_id"] != project_id:
            raise HTTPException(404, f"Resource not found: {revision_id}")
        project = store.get_project(revision["project_id"])

        # Reject settings that name a model ComfyUI does not have before a job is
        # queued (spec section 8). An unreachable ComfyUI is a 409 rather than a
        # 5xx: the public proxy replaces 5xx bodies, and the dialog must be able
        # to show the message. A reachable ComfyUI that errors is a 502.
        client = comfy_client_factory()
        try:
            options = _comfy_options(client)
        except ComfyUnavailable as exc:
            raise HTTPException(409, COMFY_NOT_RUNNING) from exc
        except ComfyUIError as exc:
            raise HTTPException(502, str(exc)) from exc
        finally:
            client.close()
        invalid = _validate_upscale_settings(payload.settings, options)
        if invalid is not None:
            raise HTTPException(422, invalid)

        async with admission_lock:
            if store.active_job_count() >= config.max_active_jobs:
                raise HTTPException(429, "Too many active jobs; try again after one finishes")
            job = store.create_job(kind="upscale", project_id=project["id"],
                                   revision_id=revision_id)
        jobs.start_upscale(job["id"], parent_revision_id=revision_id, settings=payload.settings)
        return {"project": project, "job": job}

    @app.get("/api/upscale/options", response_model=UpscaleOptions)
    async def upscale_options() -> dict:
        def option(value: str) -> dict:
            return {"value": value, "label": value}

        # An unreachable ComfyUI is an expected state, not a server fault: report
        # it as `available: false` so the dialog can say so. A reachable ComfyUI
        # that errors is a 502.
        client = comfy_client_factory()
        try:
            options = _comfy_options(client)
        except ComfyUnavailable:
            return {"available": False}
        except ComfyUIError as exc:
            raise HTTPException(502, str(exc)) from exc
        finally:
            client.close()
        return {
            "available": True,
            "upscalers": [{"value": "none", "label": "None"},
                          {"value": "lanczos", "label": "Lanczos"},
                          *(option(name) for name in options["upscalers"])],
            "checkpoints": [option(name) for name in options["checkpoints"]],
            "controlnets": [option(name) for name in options["controlnets"]],
            "defaults": UpscaleSettings().model_dump(),
            "sample_size_max": SAMPLE_SIZE_MAX,
        }

    @app.get("/api/projects/{project_id}", response_model=ProjectDetail)
    async def get_project(project_id: str) -> dict:
        try:
            return {**store.get_project(project_id), "revisions": await asyncio.to_thread(
                served_revisions, store, store.list_revisions(project_id))}
        except KeyError as exc:
            raise missing(exc) from exc

    @app.delete("/api/projects/{project_id}", status_code=204, response_class=Response)
    async def delete_project(project_id: str) -> Response:
        try:
            store.delete_project(project_id)
        except KeyError as exc:
            raise missing(exc) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    @app.post("/api/projects/{project_id}/revisions", response_model=Revision, status_code=201)
    async def create_revision(project_id: str, payload: CreateRevision) -> dict:
        try:
            return store.create_revision(
                project_id, parent_revision_id=payload.parent_revision_id, kind=payload.kind,
                message=payload.message, settings=payload.settings,
            )
        except KeyError as exc:
            raise missing(exc) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/projects/{project_id}/revisions", response_model=list[Revision])
    async def list_revisions(project_id: str) -> list[dict]:
        try:
            return await asyncio.to_thread(
                served_revisions, store, store.list_revisions(project_id))
        except KeyError as exc:
            raise missing(exc) from exc

    @app.get("/api/revisions/{revision_id}/artifacts", response_model=list[Artifact])
    async def list_artifacts(revision_id: str) -> list[Artifact]:
        try:
            return [artifact(row) for row in store.list_artifacts(revision_id)]
        except KeyError as exc:
            raise missing(exc) from exc

    @app.get("/api/revisions/{revision_id}/effective-artifacts", response_model=list[Artifact])
    async def effective_artifacts(revision_id: str, before_cleanup: bool = False) -> list[Artifact]:
        # ``before_cleanup`` resolves the raster the cleanup stage starts from, which
        # the Flattened and Edited views show while a cleanup is active.
        try:
            return [artifact(row) for row in store.effective_artifacts(
                revision_id, before_cleanup=before_cleanup).values()]
        except KeyError as exc:
            raise missing(exc) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/revisions/{revision_id}/artifacts", response_model=Artifact, status_code=201)
    async def upload_artifact(revision_id: str, kind: str = Form(...), file: UploadFile = File(...)) -> Artifact:
        try:
            if file.size is not None and file.size > config.max_upload_bytes:
                raise HTTPException(413, "Upload exceeds the configured limit")
            return artifact(store.add_artifact(
                revision_id, kind=kind, filename=file.filename or "artifact",
                media_type=file.content_type or "application/octet-stream", source=file.file,
            ))
        except KeyError as exc:
            raise missing(exc) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                409,
                "an artifact with this kind and filename already exists for this revision",
            ) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/revisions/{revision_id}/inkscape-handoff")
    async def inkscape_handoff(revision_id: str) -> dict:
        try:
            effective = store.effective_artifacts(revision_id)
            exports = [artifact(row) for row in effective.values()
                       if row["media_type"] in {"image/svg+xml", "image/png"}
                       and row["kind"] in {
                           "traced-overlap", "traced-edge", "traced-preview",
                           "inkscape-svg", "inkscape-preview",
                       }]
            return {
                "exports": exports,
                "shared_folder_enabled": config.inkscape_shared_dir is not None,
                "guidance": (
                    "Choose an SVG or PNG export explicitly. Edit an SVG in desktop Inkscape, "
                    "then upload it here. Remote and phone sessions cannot launch desktop "
                    "Inkscape directly."
                ),
            }
        except KeyError as exc:
            raise missing(exc) from exc

    @app.post("/api/revisions/{revision_id}/inkscape-save")
    async def save_for_inkscape(revision_id: str, artifact_id: str = Form(...)) -> dict:
        try:
            return inkscape.save_shared(revision_id, artifact_id)
        except KeyError as exc:
            raise missing(exc) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/projects/{project_id}/revisions/{revision_id}/inkscape-reimport",
              response_model=Revision, status_code=201)
    async def reimport_inkscape(project_id: str, revision_id: str,
                                svg: UploadFile = File(...)) -> dict:
        if svg.size is not None and svg.size > config.max_upload_bytes:
            raise HTTPException(413, "Upload exceeds the configured limit")
        try:
            return await asyncio.to_thread(
                inkscape.reimport, project_id, revision_id,
                filename=svg.filename or "edited.svg", source=svg.file)
        except KeyError as exc:
            raise missing(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/revisions/{revision_id}/editor", response_model=EditorState)
    async def get_editor_state(revision_id: str) -> dict:
        try:
            return await asyncio.to_thread(editor_state, store, revision_id)
        except KeyError as exc:
            raise missing(exc) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/revisions/{revision_id}/automatic-layer-order",
             response_model=AutomaticLayerOrder)
    async def get_automatic_layer_order(revision_id: str) -> dict:
        try:
            return {"automatic_layer_order": await asyncio.to_thread(
                automatic_layer_order, store, revision_id)}
        except KeyError as exc:
            raise missing(exc) from exc

    @app.post("/api/projects/{project_id}/revisions/{revision_id}/edits",
              response_model=Revision, status_code=201)
    async def edit_labels(project_id: str, revision_id: str, payload: ApplyLabelEdits) -> dict:
        try:
            result_id = await asyncio.to_thread(
                apply_edits, store, project_id, revision_id, payload.operations, payload.message)
            return store.get_revision(result_id)
        except KeyError as exc:
            raise missing(exc) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/artifacts/{artifact_id}/content")
    async def artifact_content(artifact_id: str) -> StreamingResponse:
        try:
            row = store.get_artifact(artifact_id)
            path = store.artifact_path(artifact_id)

            async def chunks():
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        yield chunk

            return StreamingResponse(
                chunks(), media_type=row["media_type"],
                headers={"Content-Disposition": f'attachment; filename="{row["filename"]}"'},
            )
        except KeyError as exc:
            raise missing(exc) from exc

    @app.post("/api/jobs", response_model=Job, status_code=202)
    async def start_job(payload: StartJob) -> dict:
        try:
            if payload.kind != "foundation-demo" and (not payload.project_id or not payload.revision_id):
                raise HTTPException(422, "Pipeline jobs require project_id and revision_id")
            cleanup = payload.cleanup.model_dump() if payload.cleanup else None
            if payload.rerun_last_cleanup:
                recorded = await asyncio.to_thread(
                    rerun_cleanup_settings, store, payload.revision_id)
                cleanup = CleanupJobSettings(**recorded).model_dump()
            job = await admit_job(kind=payload.kind, project_id=payload.project_id,
                                  revision_id=payload.revision_id)
            if payload.kind == "foundation-demo":
                jobs.start_demo(job["id"], steps=payload.steps, step_delay_ms=payload.step_delay_ms)
            else:
                jobs.start_pipeline(
                    job["id"], kind=payload.kind, project_id=payload.project_id or "",
                    revision_id=payload.revision_id or "",
                    palette=payload.palette.model_dump() if payload.palette else None,
                    trace=payload.trace.model_dump() if payload.trace else None,
                    cleanup=cleanup,
                    colour_names=payload.colour_names)
            return job
        except KeyError as exc:
            raise missing(exc) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/jobs/{job_id}", response_model=Job)
    async def get_job(job_id: str) -> dict:
        try:
            return store.get_job(job_id)
        except KeyError as exc:
            raise missing(exc) from exc

    @app.post("/api/jobs/{job_id}/cancel", response_model=Job)
    async def cancel_job(job_id: str) -> dict:
        try:
            return await jobs.cancel(job_id)
        except KeyError as exc:
            raise missing(exc) from exc

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request) -> StreamingResponse:
        try:
            store.get_job(job_id)
        except KeyError as exc:
            raise missing(exc) from exc

        async def events():
            async for event in jobs.subscribe(job_id):
                if await request.is_disconnected():
                    break
                if event.get("keepalive"):
                    yield ": keepalive\n\n"
                else:
                    yield f"event: job\ndata: {json.dumps(event)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    frontend = Path(__file__).resolve().parents[2] / "web" / "dist"
    if frontend.exists():
        @app.get("/{frontend_path:path}", include_in_schema=False)
        async def frontend_file(frontend_path: str) -> StreamingResponse:
            requested = (frontend / frontend_path).resolve()
            if not requested.is_relative_to(frontend.resolve()) or not requested.is_file():
                requested = frontend / "index.html"

            async def chunks():
                with requested.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        yield chunk

            media_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
            return StreamingResponse(chunks(), media_type=media_type)
    return app


def __getattr__(name: str) -> FastAPI:
    """Build the ASGI ``app`` only when the server actually accesses it.

    Importing this module to reach ``create_app`` (as the test suite does) must not
    construct a ``Store``, because ``create_app`` with no config resolves the data
    directory from the environment and would open the deployed database as a side
    effect of a mere import.
    """
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
