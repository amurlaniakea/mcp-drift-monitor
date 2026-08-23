FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/amurlaniakea/mcp-drift-monitor"
LABEL org.opencontainers.image.description="Continuous MCP Registry Drift Monitor with content-binding revalidation"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"

# Create a non-root user for security
RUN groupadd -r -g 1001 appuser && \
    useradd -r -u 1001 -g appuser -m -s /bin/bash appuser

WORKDIR /app

# pyproject.toml is the SINGLE SOURCE OF TRUTH for runtime deps (requests + typer)
# AND the package metadata. We install from it (never a hardcoded pip line) so the
# image and pyproject can never drift apart.
#
# BUG #5 fix: the source tree AND pyproject MUST be present before `pip install .`,
# otherwise pip builds an EMPTY wheel (the code was not copied yet) and installs a
# "mcp-drift-monitor" package with no modules — the `mcp-drift-monitor` entry point
# then fails with ModuleNotFoundError. So we COPY pyproject + code + data FIRST, then
# install. (This trades away a separate dependency-cache layer; correctness wins.)
COPY pyproject.toml ./
COPY mcp_drift_monitor/ mcp_drift_monitor/

# Embed the calibration panel so `replay` works out of the box.
# Decide: panel is BAKED INTO THE IMAGE (simple, reproducible). If you prefer to
# keep a newer panel without rebuilding, mount it over the volume instead:
#   docker run -v /path/to/panel:/app/data amurlaniakea/mcp-drift-monitor:local \
#     replay --panel-path data/mcp_registry_drift_panel_v1.jsonl
COPY data/ data/

# Install the package (with real modules) + its deps from pyproject.toml.
# [test] extras (pytest/ruff) are deliberately NOT installed in the image.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Create data directory for the SQLite database (managed by state.py).
# /app/data is the PERSISTENT volume: pass --db /app/data/drift.sqlite so the
# state survives container restarts (the default --db is "drift.sqlite", which
# resolves to /app/drift.sqlite OUTSIDE the volume and would be lost on restart).
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Volume declaration for persistent SQLite database storage.
# Mount a named volume or host dir here AND pass --db /app/data/drift.sqlite:
#   docker run -v mcp-data:/app/data ... --db /app/data/drift.sqlite
VOLUME ["/app/data"]

ENTRYPOINT ["python", "-m", "mcp_drift_monitor.cli"]
