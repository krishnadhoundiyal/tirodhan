"""Run inside the built sidecar image against a local HTTP stub, never Azure.

Production config is reused except test-only endpoint/TLS and ephemeral buffer
paths. This exercises the actual pinned plugin and final flush, not just mocks.
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from deployment import sidecar
from deployment.coordination import write_marker
from deployment.sidecar import supervise


def probe(reason: str) -> None:
    requests: list[tuple[str, bytes]] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(("PUT", body))
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            requests.append(("GET", b""))
            self.send_response(403)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass  # Never print SAS-bearing request URLs.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            config = Path("/source/tirodhan.conf").read_text()
            config = config.replace("${LOG_SHARED_DIR}", name)
            config = config.replace("${LOG_ACCOUNT_NAME}", "synthetic")
            config = config.replace("${LOG_WORKLOAD}", "probe")
            config = config.replace("${LOG_SAS}", "sv=2023-11-03&sp=w&sig=synthetic-placeholder")
            config = re.sub(r"(?m)^    Tls\s+On$", "    Tls                   Off", config)
            config = config.replace("    Tls.Verify            On", "    Tls.Verify            Off")
            config = config.replace("    Tls.Verify_Hostname   On", "    Tls.Verify_Hostname   Off")
            config += (
                f"\n    Emulator_Mode On\n    Endpoint http://127.0.0.1:{server.server_port}\n"
            )
            config = config.replace("/tmp/tirodhan-blob", f"{name}/buffer")
            configuration = directory / "fluent-bit.conf"
            configuration.write_text(config)
            (directory / "application.jsonl").write_text(
                json.dumps({"message": "probe_event"}) + "\n"
            )
            write_marker(directory / "heartbeat.json", {"timestamp": time.time()})

            def finish() -> None:
                time.sleep(2.5)  # Let the actual tail input discover the file.
                if reason == "done":
                    write_marker(
                        directory / "done.json", {"exit_code": 0, "timestamp": time.time()}
                    )
                else:
                    write_marker(directory / "heartbeat.json", {"timestamp": time.time() - 60})

            thread = threading.Thread(target=finish)
            thread.start()
            started = time.monotonic()
            original = subprocess.Popen
            diagnostics = directory / "diagnostics"
            with diagnostics.open("wb") as diagnostic:

                def captured(*args: object, **kwargs: object) -> subprocess.Popen:
                    kwargs.update(stdout=diagnostic, stderr=diagnostic)
                    return original(*args, **kwargs)

                sidecar.subprocess.Popen = captured
                try:
                    code = supervise(
                        ["/fluent-bit/bin/fluent-bit", "-c", str(configuration)],
                        directory,
                        is_job=True,
                        grace_seconds=15,
                        poll_seconds=0.1,
                    )
                finally:
                    sidecar.subprocess.Popen = original
            thread.join(timeout=5)
            if code:
                # This probe uses ONLY synthetic SAS and a local HTTP stub.
                print(diagnostics.read_text().replace("synthetic-placeholder", "REDACTED"))
            assert code == 0, "Pinned Fluent Bit runtime failed"
            assert time.monotonic() - started < 20, "Sidecar exceeded bounded flush contract"
            assert requests and all(method == "PUT" for method, _ in requests), (
                "Plugin required non-write access despite auto_create_container off"
            )
            payloads = [
                gzip.decompress(body) for _, body in requests if body.startswith(b"\x1f\x8b")
            ]
            assert any(b"probe_event" in body for body in payloads), (
                "Final buffered JSON not shipped"
            )
            print(f"PASS: {reason} shutdown, compressed final flush, write-only requests")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    probe("done")
    probe("stale")
