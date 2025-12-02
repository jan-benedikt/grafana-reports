# Grafana Reporting Service

Comprehensive guide for the modular Grafana PDF exporter that powers scheduled email delivery and the optional FastAPI endpoint for ad‑hoc downloads.

## 1. Overview
- Renders Grafana dashboards to PDF via Playwright/Chromium with kiosk CSS tweaks, header banner, and automatic sizing ("auto", "a4", or CSS).
- Sends the generated PDFs over SMTP or SendGrid; supports plaintext or fully branded HTML emails with inline images.
- Schedules recurring jobs with APScheduler and exposes `uvicorn reporting.api:app` for per-dashboard HTTP exports.

## 2. Requirements
- Python 3.10+
- Google Chrome/Chromium dependencies for Playwright (run `playwright install chromium` once per environment).
- Access to Grafana dashboard URLs (service account or API key) and optional SMTP/SendGrid credentials.

## 3. Installation & Environment Setup
1. Create and activate a virtualenv.
2. Install dependencies (example): `pip install -r requirements.txt` (ensure `playwright`, `apscheduler`, `jinja2`, `python-dateutil`, `fastapi`, `uvicorn[standard]`, `requests`).
3. Install Playwright browser binaries: `playwright install chromium`.
4. Export environment variables described below or use a `.env` file + loader of your choice.

## 4. Configuration File (`config.yml`)
```
timezone: Europe/Prague
reports:
  - name: Production VMS Monthly
    dashboard_uid: abc123
    vars:
      site: prg
    from: now-30d
    to: now
    cron: "0 6 1 * *"
    email:
      to: ["ops@example.com"]
      subject: "{{ name }} ({{ period_from_human }} → {{ period_to_human }})"
      body: "Attached PDF covers {{ vars.site }}"
      body_html: |
        <div style="font-family:sans-serif">
          <img src="cid:logo.png" alt="Logo" width="180" />
          <p>Period: {{ period_from_human }} → {{ period_to_human }}</p>
        </div>
      filename_prefix: vms-prd
```
Key notes:
- `cron` uses standard crontab syntax in the target timezone.
- `vars` become `var-*` query params for Grafana.
- `body_html` is optional; falls back to plaintext.

## 5. CLI Usage (`grafana_report.py`)
```
./grafana_report.py -c config.yml             # start scheduler
./grafana_report.py --run-once "PRD VMS"     # generate single report immediately
./grafana_report.py --run-all-now             # generate every report once then exit
./grafana_report.py --daemon --api-port 9000  # run scheduler + FastAPI in one process
```
- Scheduler logs show `[SCHEDULED]`, `[SENT]`, or `[ERROR]` per cron run.
- Use `CTRL+C` to stop the scheduler gracefully.
- `--daemon` also boots the FastAPI server (defaults host `0.0.0.0`, port `8080`). Use `--api-host`/`--api-port` to override.

## 6. FastAPI Endpoint
Start the server:
```
uvicorn reporting.api:app --reload --port 8080
```
Download a PDF:
```
curl -L "http://localhost:8080/abc123?from=now-2d&to=now&timezone=Europe/Prague" --output dashboard.pdf
```
- No emails are sent from API-triggered runs; callers receive the PDF directly.

## 7. Email Delivery Options
### Common switches
- `EMAIL_BACKEND=smtp` (default) or `sendgrid`.
- `REPORTS_EMAIL_ENABLED=true` to enable sending.

### SMTP env vars
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`.

### SendGrid env vars
`SENDGRID_API_KEY`, optional `SENDGRID_FROM`, `SENDGRID_REPLY_TO`.

### Template controls
| Variable | Description |
|---|---|
| `EMAIL_TEMPLATE_MODE` | `plain` (default) or `html`. `html` loads a shared template when a report omits `body_html`. |
| `EMAIL_TEMPLATE_HTML_PATH` | Path to a Jinja2 HTML file (used when `EMAIL_TEMPLATE_MODE=html`). |
| `EMAIL_TEMPLATE_IMAGES_DIR` | Directory of inline assets; filenames become content IDs (`cid:logo.png`). |

Per-report `email.body_html` overrides the shared template. Context provides `name`, `vars`, `period_*`, plus `text_body`/`html_body` for nesting.

## 8. PDF Rendering Controls
| Variable | Purpose |
|---|---|
| `PDF_SIZE_MODE` | `auto`, `a4`, or `css` for page sizing. |
| `PDF_LANDSCAPE`, `PDF_PAPER_FORMAT` | Traditional print options. |
| `WAIT_EXTRA_MS`, `READY_TIMEOUT_MS` | Control how long the renderer waits for panels. |
| `PDF_FAST_MODE` | Skips heavy scrolling and relies on spinner detection for small dashboards. |
| `PDF_OUTDIR` | Directory where PDFs are written before emailing. |

## 9. Troubleshooting
- **Table headers missing**: ensure kiosk CSS is enabled (`FORCE_KIOSK_CSS=1`) and that the Grafana theme hasn't overridden table components.
- **Blank last page**: keep `PDF_SIZE_MODE=auto` so the renderer clamps to actual DOM height; the code also enforces `page_ranges="1"`.
- **Playwright login loops**: verify `GRAFANA_USER`/`GRAFANA_PASSWORD` are correct and that the Grafana user has proper dashboard access permissions.
- **Email template issues**: confirm paths exist and HTML references `cid` that match filenames inside `EMAIL_TEMPLATE_IMAGES_DIR`.

Refer to `grafana_report.py` comments and the modules under `reporting/` for deeper details.

## 10. Docker Image
1. Build the image (run inside `grafana_reports/`):
  ```bash
  docker build -t grafana-reports .
  ```
2. Prepare an env file (or `-e` flags) with Grafana/SMTP/SendGrid secrets.
3. Run the container; it defaults to `--daemon` (scheduler + FastAPI on 8080):
  ```bash
  docker run -d --name grafana-reports \
    --env-file ./env.prod \
    -v $(pwd)/config.yml:/app/config.yml:ro \
    -p 8080:8080 \
    grafana-reports
  ```
4. Override behaviour by appending CLI flags, for example:
  ```bash
  docker run --rm grafana-reports --run-once "Production VMS Monthly"
  ```

### Notes
- The image is based on the Microsoft Playwright runtime and installs Chromium during build, so no extra host dependencies are required.
- Mount a writable directory to `/app/output` (or set `PDF_OUTDIR`) if you want to persist PDFs beyond email attachments.
- Remember to expose any custom FastAPI port you choose via `-p <host>:<container>`.
