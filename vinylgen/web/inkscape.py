from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from vinylgen.preview import preview_plan
from typing import BinaryIO

from .store import Store


SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
ACTIVE_ELEMENTS = {"script", "foreignobject", "iframe", "object", "embed", "audio", "video",
                   "animate", "animatemotion", "animatetransform", "set"}
CSS_URL = re.compile(r"url\s*\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\r\n\f]?|([^\r\n\f]))")
XML_DECLARATION = re.compile(r"\A<\?xml(?=[ \t\r\n])[^?]*\?>")


def _canonical_css(value: str) -> str:
    """Remove CSS lexical obfuscation before checking references."""
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL)

    def replace(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            codepoint = int(match.group(1), 16)
            return "\ufffd" if codepoint == 0 or codepoint > 0x10FFFF else chr(codepoint)
        return match.group(2) or ""

    return CSS_ESCAPE.sub(replace, value)


def _validate_css(value: str) -> None:
    value = _canonical_css(value)
    lowered = value.lower()
    if "@import" in lowered or "expression(" in lowered or "javascript:" in lowered:
        raise ValueError("active or external SVG styles are not accepted")
    for match in CSS_URL.finditer(value):
        if not match.group(2).strip().startswith("#"):
            raise ValueError("external SVG style references are not accepted")


def _reject_processing_instructions(payload: bytes) -> None:
    """Allow a leading XML declaration, but reject every other XML PI.

    ElementTree normally discards processing instructions, so this check must
    happen on the original document rather than on the parsed element tree.
    Inkscape emits UTF-8 SVG; rejecting other encodings also prevents a
    multi-byte encoding from hiding the ``<?`` delimiter from this check.
    """
    try:
        document = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("SVG must use UTF-8 encoding") from exc
    declaration = XML_DECLARATION.match(document)
    remainder = document[declaration.end():] if declaration else document
    if "<?" in remainder:
        raise ValueError("SVG processing instructions are not accepted")


def validate_safe_svg(payload: bytes) -> dict[str, int | str]:
    _reject_processing_instructions(payload)
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("SVG document types and entities are not accepted")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"invalid SVG XML: {exc}") from exc
    if root.tag != f"{{{SVG}}}svg":
        raise ValueError("uploaded file is not an SVG document")
    nodes = 0
    for element in root.iter():
        nodes += 1
        local_tag = element.tag.rsplit("}", 1)[-1].lower()
        if local_tag in ACTIVE_ELEMENTS:
            raise ValueError(f"active SVG element is not accepted: {local_tag}")
        if local_tag == "style":
            _validate_css(element.text or "")
        for name, value in element.attrib.items():
            local = name.rsplit("}", 1)[-1].lower()
            if local.startswith("on"):
                raise ValueError("SVG event handlers are not accepted")
            if local == "style":
                _validate_css(value)
            # Paint/filter/marker and other presentation attributes can carry
            # url(...) just as a style declaration can.
            _validate_css(value)
            if local == "href":
                target = value.strip().lower()
                if target and not (target.startswith("#") or
                                   target.startswith(("data:image/png", "data:image/jpeg",
                                                      "data:image/webp", "data:image/gif"))):
                    raise ValueError("external SVG references are not accepted")
    return {"root": "svg", "nodes": nodes}


class InkscapeHandoff:
    def __init__(self, store: Store):
        self.store = store

    def _render(self, payload: bytes) -> tuple[bytes, dict[str, object]]:
        validate = validate_safe_svg(payload)
        project = Path(__file__).resolve().parents[2]
        runner = project / "scripts" / "inkscape-headless"
        with tempfile.TemporaryDirectory(dir=self.store.config.data_dir) as directory:
            work = Path(directory)
            source, preview = work / "edited.svg", work / "traced.png"
            source.write_bytes(payload)
            try:
                plan = preview_plan(payload)
            except ValueError as error:
                raise ValueError(f"SVG preview size cannot be determined: {error}") from error
            command = [str(runner), str(source), "--export-type=png", *plan["arguments"],
                       f"--export-filename={preview}"]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
            if completed.returncode or not preview.is_file():
                detail = (completed.stderr or completed.stdout).strip()
                raise ValueError(f"Inkscape could not render the SVG: {detail or 'no preview produced'}")
            audit = {**validate, "inkscape_exit_code": completed.returncode,
                     "preview_dpi": plan["dpi"], "preview_width": plan["width"],
                     "preview_height": plan["height"], "preview_max_side": plan["max_side"],
                     "preview_capped": plan["capped"], "preview_bytes": preview.stat().st_size}
            return preview.read_bytes(), audit

    def reimport(self, project_id: str, revision_id: str, *, filename: str,
                 source: BinaryIO) -> dict:
        parent = self.store.get_revision(revision_id)
        if parent["project_id"] != project_id:
            raise ValueError("revision belongs to another project")
        payload = source.read(self.store.config.max_upload_bytes + 1)
        if len(payload) > self.store.config.max_upload_bytes:
            raise ValueError("SVG exceeds the configured upload limit")
        preview, audit = self._render(payload)
        revision = self.store.create_revision(
            project_id, parent_revision_id=revision_id, kind="inkscape-edit",
            message="Reimported SVG edited externally in Inkscape",
            settings={"source_filename": Path(filename).name, "validation": audit,
                      "imported_artifact_kind": "inkscape-svg",
                      "stale_concerns": ["trace", "validation", "export"]})
        self.store.add_artifact(revision["id"], kind="inkscape-svg",
                                filename="inkscape-edited.svg", media_type="image/svg+xml",
                                source=io.BytesIO(payload))
        self.store.add_artifact(revision["id"], kind="inkscape-preview",
                                filename="inkscape-preview.png", media_type="image/png",
                                source=io.BytesIO(preview))
        self.store.add_artifact(revision["id"], kind="inkscape-audit",
                                filename="inkscape-reimport.json", media_type="application/json",
                                source=io.BytesIO(json.dumps(audit, indent=2).encode()))
        return revision

    def save_shared(self, revision_id: str, artifact_id: str) -> dict[str, str]:
        shared = self.store.config.inkscape_shared_dir
        if shared is None:
            raise ValueError("no Inkscape shared folder is configured")
        artifact = self.store.get_artifact(artifact_id)
        effective = self.store.effective_artifacts(revision_id)
        if artifact["media_type"] not in {"image/svg+xml", "image/png"} or not any(
                row["id"] == artifact_id for row in effective.values()):
            raise ValueError("artifact is not an effective SVG or PNG for this revision")
        shared.mkdir(parents=True, exist_ok=True)
        project = self.store.get_project(self.store.get_revision(revision_id)["project_id"])
        destination = shared / f"{project['slug']}-{Path(artifact['filename']).name}"
        temporary = destination.with_suffix(destination.suffix + ".incoming")
        temporary.write_bytes(self.store.artifact_path(artifact_id).read_bytes())
        os.replace(temporary, destination)
        return {"filename": destination.name, "shared_path": str(destination)}
