"""Prepare the replica volume as root, then permanently drop to the application user."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    log_path = os.environ.get("TIRODHAN_LOG_FILE_PATH")
    if sys.platform != "win32" and os.getuid() == 0:
        import pwd

        account = pwd.getpwnam("tirodhan")
        if log_path:
            directory = Path(log_path).parent
            directory.mkdir(parents=True, exist_ok=True)
            os.chown(directory, account.pw_uid, account.pw_gid)
            directory.chmod(0o755)
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    if not sys.argv[1:]:
        raise SystemExit("A workload command is required")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
