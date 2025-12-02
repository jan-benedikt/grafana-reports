# Grafana Reports

Modular Playwright-based exporter that schedules Grafana dashboards, emails PDFs, and exposes a FastAPI endpoint for ad‑hoc downloads.

## Features
- **Accurate PDFs**: waits for panel readiness, injects kiosk CSS, auto-detects dimensions, and supports A4/CSS sizing.
- **Flexible delivery**: SMTP or SendGrid, plaintext or branded HTML templates with inline images, per-report Jinja subjects/bodies.
- **Multiple entry points**: CLI for scheduler / batch runs, FastAPI (`uvicorn reporting.api:app`) for API-triggered exports.
- **Config-driven**: YAML file declares all reports, time ranges, Grafana variables, and email metadata.

## Quick Start
1. Install dependencies (virtualenv recommended):
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```
2. Copy `config.yml` and adjust `reports` + `timezone`.
3. Export env vars (Grafana auth, email backend, PDF tuning). Example:
   ```bash
   export GRAFANA_URL="https://grafana.example.com"
   export GRAFANA_USER="report-user"
   export GRAFANA_PASSWORD="secret"
   export REPORTS_EMAIL_ENABLED=1
   export EMAIL_BACKEND=smtp
   export SMTP_HOST=mail.example.com
   ```
4. Run once or start the scheduler:
   ```bash
   ./grafana_report.py --run-once "My Report"
   ./grafana_report.py -c config.yml
   ./grafana_report.py --daemon --api-port 9000  # scheduler + FastAPI together
   ```
5. Optional: serve ad-hoc PDFs via API:
   ```bash
   uvicorn reporting.api:app --port 8080
   ```

## Docker
Build the container (from the `grafana_reports` directory):
```bash
docker build -t grafana-reports .
```

Run in daemon mode (scheduler + FastAPI). Mount your config and provide env vars or an env file:
```bash
docker run -d --name grafana-reports \
   -v $(pwd)/config.yml:/app/config.yml:ro \
   --env-file .env \
   -p 8080:8080 \
   grafana-reports
```
- Defaults to `--daemon`, so FastAPI listens on port 8080 inside the container.
- Override behaviour by appending CLI flags, e.g. `docker run grafana-reports --run-once "My Report"`.

## Documentation
Full setup, configuration, and troubleshooting guide: [`docs/grafana-reporting.md`](docs/grafana-reporting.md)

## Contributing
- Keep new features modular inside the `reporting/` package.
- Update `docs/grafana-reporting.md` and this README when changing env vars or config formats.
- Please lint/format before submitting PRs.
