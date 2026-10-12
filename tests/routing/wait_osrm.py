"""Bounded local test readiness probe; no coordinate URL or HTTP logging."""

import socket
import time

deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        socket.create_connection(("127.0.0.1", 5000), timeout=1).close()
        break
    except OSError:
        time.sleep(0.2)
else:
    raise SystemExit("OSRM not ready")
