# Grafana Reports

Modular Playwright-based exporter that schedules Grafana dashboards, emails PDFs, and exposes a FastAPI endpoint for ad‑hoc downloads.

## Features
- **Accurate PDFs**: measures the real Grafana grid height, expands the viewport so all panels paint, and supports `paged` (breaks between panels), `auto`, `a4`, or `css` sizing.
- **Fast-opening tables**: colored Grafana cells are written as solid fills instead of one gradient or shadow per cell, so Acrobat does not paint them in one by one. Disable with `PDF_SIMPLIFY_TABLES=0`.
- **Secure API**: exports require an API key (`API_KEYS`); Grafana service-account tokens are supported.
- **Flexible delivery**: SMTP (STARTTLS/SSL) or SendGrid, plaintext or branded HTML templates with inline images, per-report Jinja subjects/bodies.
- **Multiple entry points**: CLI for scheduler / batch runs, FastAPI for API-triggered exports, shared Chromium process for efficiency.
- **Config-driven**: YAML file declares all reports, time ranges, Grafana variables, optional `render:` overrides, and email metadata.

## Quick Start
1. Install dependencies (virtualenv recommended):
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```
2. Copy `config.yml` and adjust `reports` + `timezone`.
3. Export env vars (Grafana auth, API keys, email backend, PDF tuning). Example:
   ```bash
   export GRAFANA_URL="https://grafana.example.com"
   export GRAFANA_USER="report-user"
   export GRAFANA_PASSWORD="secret"
   # or: export GRAFANA_TOKEN="glsa_..."
   export API_KEYS="long-random-secret"
   export REPORTS_EMAIL_ENABLED=1
   export EMAIL_BACKEND=smtp
   export SMTP_HOST=mail.example.com
   export SMTP_TLS_MODE=starttls
   export PDF_SIZE_MODE=paged
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
   curl -H "X-API-Key: $API_KEYS" "http://localhost:8080/<uid>?from=now-7d&to=now" -o report.pdf
   ```

## Docker
Build the container (from the repo root):
```bash
docker build -t grafana-reports:v1.1.0 .
```

Run in daemon mode (scheduler + FastAPI). Mount your config and provide env vars or an env file:
```bash
docker run -d --name grafana-reports \
   -v $(pwd)/config.yml:/app/config.yml:ro \
   --env-file .env \
   -p 8080:8080 \
   grafana-reports:v1.1.0
```
- Defaults to `--daemon` as non-root `pwuser`; FastAPI listens on port 8080.
- Override behaviour by appending CLI flags, e.g. `docker run grafana-reports:v1.1.0 --run-once "My Report"`.

## Tests
```bash
pytest -q
```

## Documentation
Full setup, configuration, and troubleshooting guide: [`docs/grafana-reporting.md`](docs/grafana-reporting.md)

## Contributing
- Keep new features modular inside the `reporting/` package.
- Update `docs/grafana-reporting.md` and this README when changing env vars or config formats.
- Please lint/format before submitting PRs.
