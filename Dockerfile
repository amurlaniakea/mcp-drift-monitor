FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/amurlaniakea/mcp-drift-monitor"
LABEL org.opencontainers.image.description="Continuous MCP Registry Drift Monitor with content-binding revalidation"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"

# Create a non-root user for security
RUN groupadd -r -g 1001 appuser && \
    useradd -r -u 1001 -g appuser -m -s /bin/bash appuser

WORKDIR /app

# Copy project files
COPY pyproject.toml ./

# Install dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "requests>=2.31" "typer>=0.12"

# Copy source code
COPY mcp_drift_monitor/ mcp_drift_monitor/

# Create data directory for SQLite database (managed by state.py)
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Volume declaration for persistent SQLite database storage
VOLUME ["/app/data"]

ENTRYPOINT ["python", "-m", "mcp_drift_monitor.cli"]