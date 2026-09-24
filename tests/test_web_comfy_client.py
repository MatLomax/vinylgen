from __future__ import annotations

import asyncio
import json
import socket
import unittest

import httpx

from vinylgen.web.comfy_client import ComfyClient, ComfyUIError, ComfyUnavailable


def _json_response(payload: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


class ComfyClientHappyPathTest(unittest.TestCase):
    """upload -> prompt -> await_history -> output_images -> download_image."""

    def setUp(self) -> None:
        self.paths_seen: list[str] = []
        self.poll_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            self.paths_seen.append(request.url.path)
            if request.url.path == "/upload/image":
                return _json_response({"name": "uploaded.png", "subfolder": "", "type": "input"})
            if request.url.path == "/prompt":
                body = json.loads(request.content)
                self.assertEqual(body["client_id"], "client-1")
                self.assertEqual(body["prompt"], {"1": {"class_type": "X"}})
                return _json_response({"prompt_id": "prompt-1", "number": 1, "node_errors": {}})
            if request.url.path == "/history/prompt-1":
                self.poll_count += 1
                if self.poll_count < 2:
                    return _json_response({})
                return _json_response({
                    "prompt-1": {
                        "status": {"status_str": "success", "messages": []},
                        "outputs": {
                            "9": {"images": [
                                {"filename": "out.png", "subfolder": "sub", "type": "output"},
                            ]},
                        },
                    },
                })
            if request.url.path == "/view":
                self.assertEqual(request.url.params["filename"], "out.png")
                self.assertEqual(request.url.params["subfolder"], "sub")
                self.assertEqual(request.url.params["type"], "output")
                return httpx.Response(200, content=b"PNGDATA")
            return httpx.Response(404)

        self.client = ComfyClient(
            "http://fake-comfy:8188", transport=httpx.MockTransport(handler))

    def tearDown(self) -> None:
        self.client.close()

    def test_round_trip(self) -> None:
        name = self.client.upload_image(b"pngbytes", filename="source.png")
        self.assertEqual(name, "uploaded.png")

        prompt_id = self.client.submit_prompt({"1": {"class_type": "X"}}, "client-1")
        self.assertEqual(prompt_id, "prompt-1")

        entry = self.client.await_history(prompt_id, timeout=2.0, poll=0.01)
        self.assertEqual(entry["status"]["status_str"], "success")

        images = self.client.output_images(entry)
        self.assertEqual(images, [{"filename": "out.png", "subfolder": "sub", "type": "output"}])

        data = self.client.download_image(images[0])
        self.assertEqual(data, b"PNGDATA")
        self.assertGreaterEqual(self.poll_count, 2)


class ComfyClientSubmitErrorTest(unittest.TestCase):
    def test_node_errors_raise_comfyui_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({
                "node_errors": {"3": {"errors": [{"message": "missing checkpoint"}]}},
            })

        client = ComfyClient("http://fake", transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(ComfyUIError, "missing checkpoint"):
                client.submit_prompt({}, "client-1")
        finally:
            client.close()

    def test_error_key_raises_comfyui_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({"error": {"type": "prompt_no_outputs", "message": "no outputs"}})

        client = ComfyClient("http://fake", transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(ComfyUIError, "no outputs"):
                client.submit_prompt({}, "client-1")
        finally:
            client.close()


class ComfyClientHistoryErrorTest(unittest.TestCase):
    def test_error_status_raises_comfyui_error_with_message(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({
                "prompt-err": {
                    "status": {
                        "status_str": "error",
                        "messages": [["execution_error", {"exception_message": "sampler boom"}]],
                    },
                    "outputs": {},
                },
            })

        client = ComfyClient("http://fake", transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(ComfyUIError, "sampler boom"):
                client.await_history("prompt-err", timeout=2.0, poll=0.01)
        finally:
            client.close()


class ComfyClientTimeoutTest(unittest.TestCase):
    def test_await_history_times_out_when_entry_never_appears(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({})

        client = ComfyClient("http://fake", transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(TimeoutError):
                client.await_history("missing", timeout=0.05, poll=0.01)
        finally:
            client.close()


class ComfyClientCancellationTest(unittest.TestCase):
    def test_await_history_interrupts_and_raises_before_returning_result(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/interrupt":
                return httpx.Response(200, json={})
            # The job never finishes; only a cancellation should end the poll.
            return _json_response({})

        client = ComfyClient("http://fake", transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(asyncio.CancelledError):
                client.await_history("prompt-1", timeout=5.0, poll=0.01,
                                     cancelled=lambda: True)
        finally:
            client.close()
        self.assertIn("/interrupt", paths)
        self.assertNotIn("/history/prompt-1", paths)


class ComfyClientUnavailableTest(unittest.TestCase):
    def _closed_port_url(self) -> str:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return f"http://127.0.0.1:{port}"

    def test_refused_connection_raises_comfy_unavailable(self) -> None:
        url = self._closed_port_url()
        client = ComfyClient(url)
        try:
            with self.assertRaisesRegex(ComfyUnavailable, "ComfyUI not running"):
                client.submit_prompt({}, "client-1")
        finally:
            client.close()

    def test_connect_timeout_and_dropped_connection_mean_not_running(self) -> None:
        for error in (httpx.ConnectTimeout("connect timed out"),
                      httpx.RemoteProtocolError("server disconnected")):
            def fail(request: httpx.Request, error=error) -> httpx.Response:
                raise error
            client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(fail))
            try:
                with self.subTest(error=type(error).__name__):
                    with self.assertRaisesRegex(ComfyUnavailable, "ComfyUI not running"):
                        client.submit_prompt({}, "client-1")
            finally:
                client.close()

    def test_read_timeout_stays_a_request_failure(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("busy")
        client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(fail))
        try:
            with self.assertRaises(ComfyUIError) as caught:
                client.submit_prompt({}, "client-1")
            self.assertNotIsInstance(caught.exception, ComfyUnavailable)
        finally:
            client.close()

    def test_interrupt_swallows_refused_connection(self) -> None:
        client = ComfyClient(self._closed_port_url())
        try:
            client.interrupt()  # must not raise
        finally:
            client.close()

    def test_iter_progress_ends_quietly_on_unreachable_server(self) -> None:
        client = ComfyClient(self._closed_port_url())
        try:
            messages = list(client.iter_progress("client-1"))
            self.assertEqual(messages, [])
        finally:
            client.close()


class ComfyClientObjectInfoTest(unittest.TestCase):
    def test_parses_both_combo_shapes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/object_info/UpscaleModelLoader":
                return _json_response({
                    "UpscaleModelLoader": {
                        "input": {"required": {"model_name": [["a.pth", "b.pth"]]}},
                    },
                })
            if request.url.path == "/object_info/ControlNetLoader":
                return _json_response({
                    "ControlNetLoader": {
                        "input": {"required": {
                            "control_net_name": ["COMBO", {"options": ["c.safetensors"]}],
                        }},
                    },
                })
            return httpx.Response(404)

        client = ComfyClient("http://fake", transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(
                client.object_info_options("UpscaleModelLoader", "model_name"),
                ["a.pth", "b.pth"])
            self.assertEqual(
                client.object_info_options("ControlNetLoader", "control_net_name"),
                ["c.safetensors"])
        finally:
            client.close()

    def test_missing_input_returns_empty_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({"SomeNode": {"input": {"required": {}}}})

        client = ComfyClient("http://fake", transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(client.object_info_options("SomeNode", "missing"), [])
        finally:
            client.close()


class ComfyClientEnvDefaultTest(unittest.TestCase):
    def test_default_base_url_reads_comfyui_url_env(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"COMFYUI_URL": "http://example-host:1234/"}):
            client = ComfyClient()
            try:
                self.assertEqual(client.base_url, "http://example-host:1234")
            finally:
                client.close()

    def test_default_base_url_falls_back_when_env_unset(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMFYUI_URL", None)
            client = ComfyClient()
            try:
                self.assertEqual(client.base_url, "http://127.0.0.1:8188")
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
