# Grafana reporting service container image
# Keep playwright package version in sync with this base image tag.
FROM mcr.microsoft.com/playwright/python:v1.64.1-resolute

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PDF_OUTDIR=/tmp/pdfs

WORKDIR /app

# Install Python dependencies first for better caching
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# Copy application code
COPY grafana_report.py ./
COPY reporting ./reporting
COPY config.yml ./config.yml

RUN mkdir -p /tmp/pdfs \
    && chown -R pwuser:pwuser /app /tmp/pdfs

USER pwuser

EXPOSE 8080

# Default command runs scheduler + FastAPI daemon. Override CMD for custom runs.
ENTRYPOINT ["python", "grafana_report.py"]
CMD ["--daemon"]
