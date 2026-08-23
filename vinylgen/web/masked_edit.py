from __future__ import annotations

import io
import asyncio
from typing import Any, Callable

from PIL import Image, ImageOps

from .generation import MaskedEditProvider, provider_size
from .store import Store


def expand_edit_prompt(text: str, mode: str) -> str:
    action = (f"Remove the selected subject and reconstruct the scene naturally: {text.strip()}."
              if mode == "remove" else
              f"Replace only the selected area with: {text.strip()}.")
    return " ".join((
        action,
        "Match the surrounding composition, perspective, lighting, and flat hard-edged graphic style.",
        "Fill the entire editable mask cleanly. Do not add text, gradients, transparency, glow, blur, dithering, hairlines, or flecks.",
        "Return one image, not variants, a collage, grid, or split composition.",
    ))


def _png(image: Image.Image) -> io.BytesIO:
    output = io.BytesIO()
    image.save(output, "PNG")
    output.seek(0)
    return output


class MaskedEditRunner:
    def __init__(self, store: Store, providers: dict[str, MaskedEditProvider]):
        self.store = store
        self.providers = providers

    def generate(self, project_id: str, revision_id: str, *, mask_bytes: bytes,
                 prompt: str, mode: str, count: int, padding: int,
                 provider_name: str, quality: str,
                 cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        revision = self.store.get_revision(revision_id)
        if revision["project_id"] != project_id:
            raise ValueError("revision belongs to another project")
        provider = self.providers.get(provider_name)
        if provider is None or not callable(getattr(provider, "edit", None)):
            raise ValueError(f"image provider does not support masked editing: {provider_name}")
        source_row = self.store.effective_artifacts(revision_id).get("source")
        if not source_row:
            raise ValueError("revision has no source image")
        with Image.open(self.store.artifact_path(source_row["id"])) as opened:
            source = opened.convert("RGB")
        with Image.open(io.BytesIO(mask_bytes)) as opened:
            mask = opened.convert("L")
        if mask.size != source.size:
            raise ValueError("mask dimensions must exactly match the source image")
        mask = mask.point(lambda value: 255 if value else 0)
        bounds = mask.getbbox()
        if not bounds:
            raise ValueError("mask must contain at least one selected pixel")
        left, top, right, bottom = bounds
        crop_box = (max(0, left - padding), max(0, top - padding),
                    min(source.width, right + padding), min(source.height, bottom + padding))
        source_crop = source.crop(crop_box)
        mask_crop = mask.crop(crop_box)
        if source_crop.size != mask_crop.size:
            raise AssertionError("crop and mask dimensions diverged")
        expanded = expand_edit_prompt(prompt, mode)
        generated_candidates: list[tuple[Image.Image, dict[str, Any]]] = []
        for _ in range(count):
            if cancelled():
                raise asyncio.CancelledError
            result = provider.edit(image=_png(source_crop).getvalue(),
                                   mask=_png(mask_crop).getvalue(), prompt=expanded,
                                   size=provider_size(*source_crop.size), quality=quality,
                                   cancelled=cancelled)
            with Image.open(io.BytesIO(result.content)) as opened:
                generated = ImageOps.fit(opened.convert("RGB"), source_crop.size,
                                         method=Image.Resampling.LANCZOS)
            composed_crop = Image.composite(generated, source_crop, mask_crop)
            composed = source.copy()
            composed.paste(composed_crop, crop_box[:2])
            generated_candidates.append((composed, result.metadata))
        # This checkpoint is the commit boundary: an explicit cancellation must
        # not leave a review revision or artifacts, even for custom providers.
        if cancelled():
            raise asyncio.CancelledError
        settings = {"short_prompt": prompt, "expanded_prompt": expanded, "mode": mode,
                    "candidate_count": count, "padding": padding, "crop_box": crop_box,
                    "crop_size": source_crop.size, "mask_size": mask_crop.size,
                    "provider": provider_name, "quality": quality,
                    "source_revision_id": revision_id, "source_artifact_id": source_row["id"],
                    "hard_mask_composition": True,
                    "provider_metadata": [metadata for _, metadata in generated_candidates]}
        candidate_revision = self.store.create_revision(
            project_id, parent_revision_id=revision_id, kind="masked-edit-candidates",
            message=f"Masked {mode} candidates: {prompt[:160]}", settings=settings)
        self.store.add_artifact(candidate_revision["id"], kind="edit-mask",
                                filename="edit-mask.png", media_type="image/png", source=_png(mask))
        candidates = []
        for index, (composed, _) in enumerate(generated_candidates, 1):
            candidate = self.store.add_artifact(
                candidate_revision["id"], kind=f"masked-candidate-{index}",
                filename=f"candidate-{index}.png", media_type="image/png", source=_png(composed))
            flattened = composed.quantize(colors=8, method=Image.Quantize.MEDIANCUT,
                                          dither=Image.Dither.NONE).convert("RGB")
            preview = self.store.add_artifact(
                candidate_revision["id"], kind=f"masked-flattened-preview-{index}",
                filename=f"candidate-{index}-flattened.png", media_type="image/png",
                source=_png(flattened))
            candidates.append((candidate, preview))
        return {"revision": candidate_revision, "expanded_prompt": expanded,
                "candidates": candidates}

    def apply(self, project_id: str, candidate_revision_id: str, index: int) -> str:
        candidates = self.store.get_revision(candidate_revision_id)
        if candidates["project_id"] != project_id or candidates["kind"] != "masked-edit-candidates":
            raise ValueError("not a masked edit candidate revision for this project")
        artifact = next((row for row in self.store.list_artifacts(candidate_revision_id)
                         if row["kind"] == f"masked-candidate-{index}"), None)
        if not artifact:
            raise ValueError("candidate does not exist")
        parent_id = candidates["parent_revision_id"]
        applied = self.store.create_revision(
            project_id, parent_revision_id=parent_id, kind="masked-edit",
            message=f"Applied masked edit candidate {index}",
            settings={**candidates["settings"], "candidate_revision_id": candidate_revision_id,
                      "candidate_index": index, "pipeline_stage": "masked-edit",
                      "stale_concerns": ["palette", "trace", "validation", "export"]})
        with self.store.artifact_path(artifact["id"]).open("rb") as source:
            self.store.add_artifact(applied["id"], kind="source", filename="source.png",
                                    media_type="image/png", source=source)
        return applied["id"]
