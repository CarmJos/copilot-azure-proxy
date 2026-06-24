# ─────────────────────────────────────────────────────────────────────────────
#  copilot-azure-proxy  —  Docker image
# ─────────────────────────────────────────────────────────────────────────────
#  Multi-stage build with a slim production image.
# ─────────────────────────────────────────────────────────────────────────────

# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.10-slim AS builder

WORKDIR /build

# Only copy requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.10-slim

# Create a non-root user
RUN addgroup --system app && adduser --system --ingroup app --home /app --shell /sbin/nologin app

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local

# Copy application files
COPY copilot_azure_proxy.py .
COPY config.yaml .

# Ensure scripts are in PATH
ENV PATH=/root/.local/bin:$PATH

# Switch to non-root user
USER app

# Default port (overridable via --port or env var)
EXPOSE 4000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health')" || exit 1

# Default entrypoint
ENTRYPOINT ["python", "copilot_azure_proxy.py", "--config", "config.yaml"]

# Default command arguments (can be overridden)
CMD ["--host", "0.0.0.0", "--port", "4000"]
