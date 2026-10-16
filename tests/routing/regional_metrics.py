"""Linux test-container sampling; outputs aggregate numbers only."""

import os
import threading
import time
from pathlib import Path


class ProcessSampler:
    def __init__(self):
        self.peak_rss_bytes = 0
        self.peak_cgroup_bytes = 0
        self.peak_cpu_cores = 0.0
        self.cpu_seconds = 0.0
        self.samples = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=2)

    def sample(self):
        seen = {}
        previous = 0.0
        stamp = time.perf_counter()
        baseline = None
        clock = os.sysconf("SC_CLK_TCK")
        page = os.sysconf("SC_PAGE_SIZE")
        while not self.stop.is_set():
            rss = 0
            for path in Path("/proc").glob("[0-9]*/stat"):
                try:
                    stat = path.read_text()
                    fields = stat[stat.rfind(")") + 2 :].split()
                    identity = (path.parent.name, fields[19])
                    seen[identity] = (int(fields[11]) + int(fields[12])) / clock
                    rss += int(fields[21]) * page
                except (OSError, ValueError, IndexError):
                    continue
            current = sum(seen.values())
            now = time.perf_counter()
            if self.samples:
                self.peak_cpu_cores = max(self.peak_cpu_cores, (current - previous) / (now - stamp))
            if baseline is None:
                baseline = current
            self.cpu_seconds = current - baseline
            self.peak_rss_bytes = max(self.peak_rss_bytes, rss)
            for path in (
                Path("/sys/fs/cgroup/memory.current"),
                Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
            ):
                try:
                    self.peak_cgroup_bytes = max(self.peak_cgroup_bytes, int(path.read_text()))
                    break
                except (OSError, ValueError):
                    continue
            self.samples += 1
            previous, stamp = current, now
            self.stop.wait(0.05)
