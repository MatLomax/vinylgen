from __future__ import annotations

import asyncio
import io
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from PIL import Image, ImageOps, UnidentifiedImageError

from .store import Store

BASELINE_REJECTED_MOTIFS = (
    "occult eyes", "all-seeing eyes", "mystical floating eyes", "mandalas",
    "Chinese dragons", "pirates", "samurai", "trains",
)


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    media_type: str
    metadata: dict[str, Any]


class ImageProvider(Protocol):
    name: str

    def generate(self, *, prompt: str, size: str, quality: str,
                 cancelled: Callable[[], bool] = lambda: False,
                 progress: Callable[[float, str], None] | None = None) -> GeneratedImage: ...


class CodexImageProvider:
    """Generate through the installed Codex CLI without handling its credentials."""

    name = "codex"

    def __init__(self, executable: str = "codex", scratch_root: Path | None = None,
                 timeout_seconds: float = 300.0):
        self.executable = executable
        self.scratch_root = Path(scratch_root or Path(__file__).resolve().parents[2] / ".tmp")
        self.timeout_seconds = timeout_seconds

    def generate(self, *, prompt: str, size: str, quality: str,
                 cancelled: Callable[[], bool] = lambda: False,
                 progress: Callable[[float, str], None] | None = None) -> GeneratedImage:
        instruction = " ".join((
            "Use the image-generation tool to create exactly one complete full-frame PNG.",
            f"Requested canvas: {size}. Requested quality: {quality}.",
            "Create no variants, contact sheet, mask, preview, or other image files.",
            "Write the final image as output.png in the current working directory.",
            "Do not inspect or use any earlier conversation or Vinylgen image as a reference.",
            f"Artwork request: {prompt}",
        ))
        return self._run(instruction, cancelled=cancelled, progress=progress)

    def _version(self, environment: dict[str, str]) -> str:
        try:
            result = subprocess.run(
                [self.executable, "--version"], capture_output=True, text=True,
                timeout=10, check=False, env=environment)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Codex CLI is not installed or is not on the Vinylgen service PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Codex CLI version check timed out") from exc
        if result.returncode:
            raise RuntimeError("Codex CLI could not be started; verify the installation")
        version = (result.stdout or result.stderr).strip()[:120]
        try:
            auth = subprocess.run(
                [self.executable, "login", "status"], capture_output=True, text=True,
                timeout=10, check=False, env=environment)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Codex login check timed out") from exc
        if auth.returncode or "chatgpt" not in (auth.stdout + auth.stderr).lower():
            raise RuntimeError(
                "Codex is not logged in with ChatGPT; run `codex login` as the service user")
        return version

    def _run(self, instruction: str, *, cancelled: Callable[[], bool],
             progress: Callable[[float, str], None] | None = None) -> GeneratedImage:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        job_dir = Path(tempfile.mkdtemp(prefix="codex-image-", dir=self.scratch_root))
        environment = os.environ.copy()
        # Force ChatGPT-backed Codex auth rather than accidental API-key billing.
        environment.pop("OPENAI_API_KEY", None)
        try:
            version = self._version(environment)
        except BaseException:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        argv = [self.executable, "exec", "--ephemeral", "--json", "--color", "never",
                "--sandbox", "workspace-write", "--skip-git-repo-check", "--cd", str(job_dir)]
        argv.extend(("--", instruction))
        if progress:
            progress(.2, "Starting isolated Codex image session")
        process: subprocess.Popen[str] | None = None
        try:
            try:
                process = subprocess.Popen(
                    argv, cwd=job_dir, env=environment, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, start_new_session=True, bufsize=1)
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "Codex CLI is not installed or is not on the Vinylgen service PATH") from exc
            events, stderr = self._collect(process, cancelled, progress)
            if process.returncode:
                raise RuntimeError(self._failure_message(process.returncode, events, stderr))
            candidates = [path for path in job_dir.rglob("*")
                          if path.is_file() and not path.is_symlink()
                          and path.suffix.lower() == ".png"]
            if not candidates:
                raise RuntimeError(
                    "Codex completed without an image; verify login and image-generation availability")
            if len(candidates) != 1:
                raise RuntimeError(
                    f"Codex produced {len(candidates)} image files; expected exactly one")
            content = candidates[0].read_bytes()
            try:
                with Image.open(io.BytesIO(content)) as opened:
                    opened.verify()
                    if opened.format != "PNG":
                        raise RuntimeError("Codex output is not a PNG image")
                with Image.open(io.BytesIO(content)) as opened:
                    dimensions = list(opened.size)
            except (UnidentifiedImageError, OSError) as exc:
                raise RuntimeError("Codex output.png is not a decodable raster image") from exc
            if cancelled():
                raise asyncio.CancelledError
            return GeneratedImage(content, "image/png", {
                "provider": self.name, "codex_version": version,
                "invocation_mode": "exec-ephemeral-jsonl", "dimensions": dimensions,
                "event_summary": self._event_summary(events),
            })
        finally:
            if process is not None and process.poll() is None:
                self._terminate(process)
            if process is not None:
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
            shutil.rmtree(job_dir, ignore_errors=True)

    def _collect(self, process: subprocess.Popen[str], cancelled: Callable[[], bool],
                 progress: Callable[[float, str], None] | None) -> tuple[list[dict[str, Any]], str]:
        lines: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def read(name: str, stream: Any) -> None:
            try:
                for line in stream:
                    lines.put((name, line))
            finally:
                lines.put((name, None))

        threads = [threading.Thread(target=read, args=("stdout", process.stdout), daemon=True),
                   threading.Thread(target=read, args=("stderr", process.stderr), daemon=True)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.timeout_seconds
        events: list[dict[str, Any]] = []
        errors: list[str] = []
        closed: set[str] = set()
        while process.poll() is None or len(closed) < 2:
            if cancelled():
                self._terminate(process)
                raise asyncio.CancelledError
            if time.monotonic() >= deadline:
                self._terminate(process)
                raise RuntimeError(f"Codex image generation timed out after {self.timeout_seconds:g} seconds")
            try:
                source, line = lines.get(timeout=.05)
            except queue.Empty:
                continue
            if line is None:
                closed.add(source)
                continue
            if source == "stderr":
                errors.append(line.rstrip())
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                if len(events) < 500:
                    events.append(event)
                if progress and event.get("type") in {"item.started", "item.completed", "turn.started"}:
                    progress(.45, "Codex is generating the image")
        process.wait()
        return events, "\n".join(errors)[-4000:]

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        process_group = process.pid
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    @staticmethod
    def _event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
        allowed = {
            "thread.started", "turn.started", "turn.completed", "turn.failed",
            "item.started", "item.updated", "item.completed", "error",
        }
        counts: dict[str, int] = {}
        ignored = 0
        for event in events:
            kind = event.get("type")
            if kind in allowed:
                counts[kind] = counts.get(kind, 0) + 1
            else:
                ignored += 1
        return {"counts": counts, "ignored_event_count": ignored}

    @staticmethod
    def _failure_message(returncode: int, events: list[dict[str, Any]], stderr: str) -> str:
        detail = " ".join((stderr + " " + json.dumps(events[-3:])).split()).lower()
        if any(term in detail for term in ("login", "authentication", "unauthorized", "401")):
            return "Codex is not logged in or its login expired; run `codex login` as the service user"
        if any(term in detail for term in ("image generation", "imagegen", "tool unavailable")):
            return "Codex image-generation capability is unavailable for this account or installation"
        return f"Codex image generation failed (exit {returncode}); check the service logs"


def provider_size(width: int, height: int) -> str:
    ratio = width / height
    if ratio > 1.15:
        return "1536x1024"
    if ratio < 0.87:
        return "1024x1536"
    return "1024x1024"


def expand_prompt(prompt: str, settings: dict[str, Any]) -> str:
    colours = settings["desired_colours"]
    required = ", ".join(item.strip() for item in settings["required_motifs"] if item.strip())
    extra_rejected = [item.strip() for item in settings["rejected_motifs"] if item.strip()]
    rejected = ", ".join(dict.fromkeys((*BASELINE_REJECTED_MOTIFS, *extra_rejected)))
    divisor = math.gcd(settings["width"], settings["height"])
    aspect_ratio = f'{settings["width"] // divisor}:{settings["height"] // divisor}'
    motif_rules = []
    if required:
        motif_rules.append(f"Required subjects, objects, or motifs: {required}.")
    motif_rules.append(
        f"Absolutely forbidden subjects or motifs: {rejected}. Do not include them as "
        "background decoration, symbols, carvings, insignia, or secondary characters.")
    motifs = "\n".join(motif_rules)
    return f"""# Generate One High-Quality Sci-Fi / Fantasy Image

Generate exactly ONE solitary, complete, full-frame image. Do not generate multiple concepts,
variants, panels, alternatives, or outputs. Do not create a contact sheet, collage, grid,
quadrants, diptych, triptych, or split-screen. Do not use any earlier generated image or
conversation image as a visual reference.

## IMAGE FORMAT

- Final delivery target after normalization: {aspect_ratio} ({settings['width']} × {settings['height']})
- Compose the full scene so its focal subject and essential details survive a centred crop to
  that target ratio
- One solitary complete scene filling the canvas
- No border, frame, caption, lettering, or typography
- One strong central focal point
- Clear foreground, midground, and background
- Strong depth through overlapping forms, scale, perspective, silhouette, and composition
- Layered subjects filling the full canvas
- Maximum visual quality and rich, intricate scene detail
- Crisp, deliberate edges and clearly resolved forms at every depth
- No tiny flecks, noisy micro-texture, hair-thin lines, or excessively delicate isolated details

## SUBJECT

Create a science-fiction or fantasy scene featuring: {prompt.strip()}

{motifs}

## STRICT {colours}-COLOUR PALETTE

The finished image must contain exactly {colours} colours total—not approximately {colours}, not
{colours} main colours plus highlights, and not {colours} colours plus darker or lighter versions.
Choose the complete {colours}-colour palette before constructing the scene, then use only those
exact {colours} discrete flat solid colours everywhere, including outlines and background. No
additional colour may appear.

Do not derive darker shades, lighter shades, intermediate shades, tints, tones, highlight colours,
shadow colours, reflected colours, atmospheric colours, edge colours, transitional colours,
glow colours, or lighting colours. If the scene needs a shadow, highlight, or atmospheric depth,
use one of the same {colours} palette colours.

Think of the construction as exactly {colours} rolls of pre-coloured adhesive vinyl. Every visible
shape must use one of those {colours} colours because no additional material exists.

## FLAT GRAPHIC STYLE

Construct the entire image from crisp hard-edged shapes, solid colour fills, bold silhouettes,
layered graphic forms, and substantial clean regions. Make it richly detailed and visually
sophisticated through deliberate shape design, internal structure, composition, and layering—not
through extra tones or noisy micro-detail. Keep edges clean and every region clearly resolved.

Absolutely no gradients, shading gradients, soft shadows, transparency, opacity effects, glow,
bloom, blur, colour blending, painterly or photographic rendering, realistic lighting,
airbrushing, textured colour variation, dithering, stippling, halftones, pixel-noise patterns,
compression-like speckling, or tiny colour flecks. Do not simulate tones with dots, broken marks,
alternating pixels, or texture. Avoid hair-thin lines and excessively delicate isolated details.

Create depth through composition, overlap, scale, perspective, silhouette, and strategic
placement of the {colours} flat colours. Prioritize maximum execution quality, intricate but clean
detail, strong visual hierarchy, and a polished professional result within this flat style.

## FINAL REQUIREMENT

Return one independent, highly detailed, maximum-quality flat graphic image made from exactly
{colours} solid colours and composed for the {aspect_ratio} final delivery target. The scene must fill the
provider canvas, remain crop-safe for that target, and be clean, coherent, and easy to separate
into colour regions."""


class GenerationRunner:
    def __init__(self, store: Store, providers: dict[str, ImageProvider]):
        self.store = store
        self.providers = providers

    def run(self, project_id: str, prompt: str, settings: dict[str, Any],
            progress: Callable[[float, str], None],
            cancelled: Callable[[], bool] = lambda: False,
            commit: Callable[[Callable[[], str]], str] = lambda operation: operation()) -> str:
        provider = self.providers.get(settings["provider"])
        if provider is None:
            raise ValueError(f"unsupported image provider: {settings['provider']}")
        expanded = expand_prompt(prompt, settings)
        requested_size = provider_size(settings["width"], settings["height"])
        progress(.15, "Sending prompt to image provider")
        result = provider.generate(prompt=expanded, size=requested_size,
                                   quality="high", cancelled=cancelled,
                                   progress=progress)
        if cancelled():
            raise asyncio.CancelledError
        progress(.82, "Normalizing generated image")
        try:
            with Image.open(io.BytesIO(result.content)) as image:
                # Preserve hard palette boundaries: interpolating resamplers invent colours.
                image = ImageOps.fit(image.convert("RGB"), (settings["width"], settings["height"]),
                                     method=Image.Resampling.NEAREST)
                output = io.BytesIO()
                image.save(output, format="PNG")
                output.seek(0)
        except (UnidentifiedImageError, OSError) as exc:
            raise RuntimeError("image provider returned a malformed image") from exc
        if cancelled():
            raise asyncio.CancelledError
        complete = {**settings, "provider_quality": "high",
                    "requested_source_colour_count": settings["desired_colours"],
                    "short_prompt": prompt,
                    "expanded_prompt": expanded,
                    "provider_metadata": result.metadata, "reference_artifact_ids": []}
        revision_id = commit(lambda: self.store.create_generated_source(
            project_id, message=f"Generated from: {prompt[:160]}",
            settings=complete, source=output))
        progress(.96, "Persisting immutable source revision")
        return revision_id
