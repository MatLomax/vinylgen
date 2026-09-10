"""Synchronous HTTP + websocket client for a local ComfyUI instance.

Mirrors the HTTP round trip the ComfyUI platform's ``scripts/generate.py``
implements against the same ComfyUI server (upload, submit, poll, download,
object_info), ported to ``httpx`` so it can run inside vinylgen's
ThreadPoolExecutor-backed job workers. The web app's generation providers are
synchronous and receive a progress callback from the caller's thread, so this
client makes no asyncio calls of its own.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable, Iterator

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8188"


class ComfyUIError(Exception):
    """A ComfyUI HTTP request or queued job failed."""


class ComfyUnavailable(ComfyUIError):
    """ComfyUI could not be reached at the configured base URL."""


class ComfyClient:
    """A thin client over one ComfyUI server's HTTP and websocket API."""

    def __init__(self, base_url: str | None = None, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = (
            base_url or os.environ.get("COMFYUI_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ComfyClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, method: str, url: str, *, timeout: float = 30.0,
                 **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, url, timeout=timeout, **kwargs)
        except httpx.ConnectError as exc:
            raise ComfyUnavailable(f"ComfyUI not running at {self.base_url}") from exc
        except httpx.TransportError as exc:
            raise ComfyUIError(f"ComfyUI request to {url} failed: {exc}") from exc

    def upload_image(self, png_bytes: bytes, filename: str = "source.png") -> str:
        """Multipart-upload a PNG to ComfyUI's input directory; return its stored name."""
        response = self._request(
            "POST", "/upload/image",
            files={"image": (filename, png_bytes, "image/png")},
        )
        data = self._json_or_raise(response, "/upload/image")
        if response.status_code >= 400 or not isinstance(data, dict) or "name" not in data:
            raise ComfyUIError(
                f"ComfyUI /upload/image failed ({response.status_code}): {data}")
        return data["name"]

    def submit_prompt(self, graph: dict, client_id: str) -> str:
        """Queue ``graph`` for execution; return the assigned prompt_id."""
        response = self._request(
            "POST", "/prompt",
            json={"prompt": graph, "client_id": client_id},
        )
        data = self._json_or_raise(response, "/prompt")
        if isinstance(data, dict) and data.get("error"):
            raise ComfyUIError(f"ComfyUI rejected prompt: {data['error']}")
        if isinstance(data, dict) and data.get("node_errors"):
            raise ComfyUIError(f"ComfyUI node errors: {json.dumps(data['node_errors'])}")
        if not isinstance(data, dict) or "prompt_id" not in data:
            raise ComfyUIError(
                f"ComfyUI /prompt response missing prompt_id ({response.status_code}): {data}")
        return data["prompt_id"]

    def await_history(self, prompt_id: str, timeout: float = 3600.0,
                       poll: float = 2.0, *,
                       cancelled: Callable[[], bool] | None = None) -> dict:
        """Poll ``/history/<prompt_id>`` until the entry appears, then return it.

        Raises ``ComfyUIError`` immediately if ComfyUI reports the job as
        failed, and ``TimeoutError`` once ``timeout`` seconds have elapsed
        without the entry appearing. A connection failure during polling
        propagates as ``ComfyUnavailable`` rather than being retried, so a
        ComfyUI crash mid-job is surfaced instead of hanging until timeout.

        When ``cancelled`` returns true the queued job is interrupted with a
        best-effort ``POST /interrupt`` and the poll stops with
        ``asyncio.CancelledError`` before any result is read, so a cancelled run
        never hands back an output.
        """
        start = time.monotonic()
        while True:
            if cancelled is not None and cancelled():
                self.interrupt()
                raise asyncio.CancelledError
            response = self._request("GET", f"/history/{prompt_id}", timeout=10.0)
            if response.status_code < 400:
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and prompt_id in data:
                    entry = data[prompt_id]
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        messages = status.get("messages", [])
                        raise ComfyUIError(
                            f"ComfyUI job {prompt_id} failed: {json.dumps(messages)}")
                    return entry
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                raise TimeoutError(
                    f"prompt {prompt_id} did not complete within {timeout}s")
            time.sleep(min(poll, max(timeout - elapsed, 0)))

    def output_images(self, history_entry: dict) -> list[dict]:
        """The ``outputs[node]["images"]`` descriptors across every output node."""
        images: list[dict] = []
        for node_output in history_entry.get("outputs", {}).values():
            for image in node_output.get("images", []):
                images.append({
                    "filename": image["filename"],
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                })
        return images

    def download_image(self, desc: dict) -> bytes:
        """Download one output image descriptor from ``/view``."""
        response = self._request(
            "GET", "/view", timeout=60.0,
            params={
                "filename": desc["filename"],
                "subfolder": desc.get("subfolder", ""),
                "type": desc.get("type", "output"),
            },
        )
        if response.status_code >= 400:
            raise ComfyUIError(
                f"ComfyUI /view failed ({response.status_code}): {response.text[:500]}")
        return response.content

    def object_info_options(self, node_class: str, input_name: str) -> list[str]:
        """The COMBO options ComfyUI reports for one input of one node class.

        ComfyUI emits two shapes for a combo: ``[[...options]]`` and
        ``["COMBO", {"options": [...]}]``. Both are read here.
        """
        response = self._request("GET", f"/object_info/{node_class}", timeout=15.0)
        data = self._json_or_raise(response, f"/object_info/{node_class}")
        if response.status_code >= 400:
            raise ComfyUIError(
                f"ComfyUI /object_info/{node_class} failed ({response.status_code}): {data}")
        spec = (data or {}).get(node_class, {}).get("input", {}) \
            .get("required", {}).get(input_name)
        if not isinstance(spec, list) or not spec:
            return []
        if isinstance(spec[0], list):
            return [str(option) for option in spec[0]]
        if len(spec) > 1 and isinstance(spec[1], dict):
            return [str(option) for option in spec[1].get("options", [])]
        return []

    def interrupt(self) -> None:
        """Best-effort ``POST /interrupt``; connection failures are swallowed."""
        try:
            self._request("POST", "/interrupt", timeout=10.0)
        except ComfyUIError:
            pass

    def iter_progress(self, client_id: str, *,
                      should_stop: Callable[[], bool] | None = None,
                      ) -> Iterator[dict | bytes]:
        """Yield decoded messages from ComfyUI's progress websocket.

        A text frame (ComfyUI's JSON status/progress/tile events) is yielded
        decoded as a dict; a binary frame (a preview image) is yielded as raw
        bytes. This is best-effort UI progress, not a completion signal: the
        generator ends quietly on any connection problem rather than raising,
        since ``await_history``'s HTTP poll is the authoritative source of
        completion. The caller runs this generator in its own thread; the
        socket is read with a short timeout so a ``should_stop`` predicate can
        end the reader between reads without waiting on a blocking receive.
        Breaking out of the iteration or calling ``.close()`` from the reader
        thread unwinds the ``with`` block below and closes the socket.
        """
        try:
            from websockets.exceptions import ConnectionClosed
            from websockets.sync.client import connect as ws_connect
        except ImportError:
            return
        try:
            with ws_connect(self._ws_url(client_id), open_timeout=10,
                             close_timeout=5) as socket:
                while True:
                    if should_stop is not None and should_stop():
                        return
                    try:
                        message = socket.recv(timeout=0.5)
                    except TimeoutError:
                        continue
                    except ConnectionClosed:
                        return
                    if isinstance(message, (bytes, bytearray)):
                        yield bytes(message)
                        continue
                    try:
                        yield json.loads(message)
                    except json.JSONDecodeError:
                        continue
        except GeneratorExit:
            raise
        except Exception:
            return

    def _ws_url(self, client_id: str) -> str:
        scheme, _, host = self.base_url.partition("://")
        ws_scheme = "wss" if scheme == "https" else "ws"
        return f"{ws_scheme}://{host}/ws?clientId={client_id}"

    @staticmethod
    def _json_or_raise(response: httpx.Response, path: str) -> Any:
        try:
            return response.json()
        except ValueError:
            raise ComfyUIError(
                f"ComfyUI {path} returned a non-JSON response "
                f"({response.status_code}): {response.text[:500]}")
