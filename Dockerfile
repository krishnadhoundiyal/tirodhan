FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/app

WORKDIR /app

RUN groupadd --system tirodhan \
    && useradd --system --gid tirodhan --home-dir /app tirodhan

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

RUN pip install --no-cache-dir . \
    && chown -R tirodhan:tirodhan /app

# The entrypoint prepares an EmptyDir mount, then drops all root privileges.
USER root
ENTRYPOINT ["python", "-m", "tirodhan.deployment.entrypoint"]

EXPOSE 8000

CMD ["uvicorn", "tirodhan.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
