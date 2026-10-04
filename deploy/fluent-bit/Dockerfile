# Debug variant supplies the package manager for the small stdlib-only coordinator.
FROM cr.fluentbit.io/fluent/fluent-bit:4.0.11-debug@sha256:a0cfa287d3d4440bf7852192d15e7eaececb9619de6069bee7ca9989d2a62eb9
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY src/tirodhan/deployment/__init__.py /opt/deployment/__init__.py
COPY src/tirodhan/deployment/coordination.py /opt/deployment/coordination.py
COPY src/tirodhan/deployment/sidecar.py /opt/deployment/sidecar.py
COPY deploy/fluent-bit/tirodhan.conf /fluent-bit/etc/tirodhan.conf
COPY deploy/fluent-bit/parsers.conf /fluent-bit/etc/tirodhan-parsers.conf
ENV PYTHONPATH=/opt
ENTRYPOINT ["python3", "-m", "deployment.sidecar"]
CMD ["service"]
