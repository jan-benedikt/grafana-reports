# Grafana reporting service container image
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

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

EXPOSE 8080

# Default command runs scheduler + FastAPI daemon. Override CMD for custom runs.
ENTRYPOINT ["python", "grafana_report.py"]
CMD ["--daemon"]
