"""Private pipe protocol for one DNS/TLS/HTTP attempt. Never print exception data."""

import base64
import json
import os
import sys
import threading

from tirodhan.modules.customer_account.web_push import post_public


def main() -> None:
    # A daemon watchdog also bounds stalled libc DNS if the supervising parent is killed.
    # getaddrinfo releases the GIL. This is a process exit, not coroutine cancellation.
    watchdog = threading.Timer(20, lambda: os._exit(1))
    watchdog.daemon = True
    watchdog.start()
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError()
        value = json.loads(raw)
        status = post_public(
            value["endpoint"], base64.b64decode(value["body"], validate=True), value["headers"]
        )
        sys.stdout.write(str(status))
    except Exception:
        # Includes DNS, HTTP, TLS and malformed private IPC. Parent retains retry debt.
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
