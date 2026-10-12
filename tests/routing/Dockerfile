# Local opt-in native tests only; never used by application deployment.
FROM ghcr.io/vroom-project/vroom-docker@sha256:247d5683d6745c755d718a156d16b16aac80baccc276a003a68b986c13883b08
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/routing && /opt/routing/bin/pip install --no-cache-dir ortools==9.14.6206 httpx==0.28.1 pytest==8.4.2 pytest-asyncio==0.26.0
ENV PATH="/opt/routing/bin:$PATH" PYTHONPATH=/repo/src PYTHONDONTWRITEBYTECODE=1
WORKDIR /repo
COPY pyproject.toml /repo/
COPY src /repo/src
COPY tests/routing /repo/tests/routing
ENTRYPOINT []
HEALTHCHECK NONE
CMD ["python", "-m", "pytest", "-q", "-s", "tests/routing", "-p", "no:cacheprovider"]
