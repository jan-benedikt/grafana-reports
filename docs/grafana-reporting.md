# Grafana Reporting Service

Comprehensive guide for the modular Grafana PDF exporter that powers scheduled email delivery and the optional FastAPI endpoint for ad‑hoc downloads.

## 1. Overview
- Renders Grafana dashboards to PDF via Playwright/Chromium with kiosk CSS tweaks, header banner, and automatic sizing (`paged`, `auto`, `a4`, or `css`).
- Measures the real dashboard grid height, expands the viewport so all panels paint, and (in `paged` mode) inserts page breaks between panels.
- Sends the generated PDFs over SMTP or SendGrid; supports plaintext or fully branded HTML emails with inline images.
- Schedules recurring jobs with APScheduler and exposes FastAPI for per-dashboard HTTP exports (API-key protected).

## 2. Requirements
- Python 3.11+ locally (`python-dateutil` 4.x); the Docker image uses Playwright `v1.64.1-resolute` (Ubuntu 26.04, Python 3.14).
- Google Chrome/Chromium dependencies for Playwright (run `playwright install chromium` once per environment).
- Access to Grafana dashboard URLs (service account / API token preferred) and optional SMTP/SendGrid credentials.

## 3. Installation & Environment Setup
1. Create and activate a virtualenv.
2. Install dependencies: `pip install -r requirements.txt`.
3. Install Playwright browser binaries: `playwright install chromium`.
4. Export environment variables described below or use a `.env` file + loader of your choice.
5. Set `API_KEYS` to one or more long random secrets (required for HTTP export endpoints).

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
    render:
      width: 1920
      page_mode: paged
      theme: light
      scale_factor: 2
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
- `from` / `to` support Grafana-style relative time: `now`, `now-30d`, `now-1M`, `now/M`, `now-1M/M`, plus epoch ms and ISO dates. Use a trailing minus for “end of period”, e.g. **`now/M-1d`** = one day before start of current month = **last day of previous month**. Example for “full previous month” (1st to 28th/29th): `from: "now-1M/M"`, `to: "now/M-1d"`. Expressions with a round suffix or minus are converted to explicit epoch timestamps in the URL so Grafana shows the correct range.
- `vars` become `var-*` query params for Grafana.
- `render` (optional) overrides global PDF settings per report: `width`, `page_mode`, `page_height_px`, `theme`, `scale_factor`.
- `body_html` is optional; falls back to plaintext.
- Config is validated at startup (UID format, cron, time range, timezone).

## 5. CLI Usage (`grafana_report.py`)
```
./grafana_report.py -c config.yml             # start scheduler
./grafana_report.py --run-once "PRD VMS"     # generate single report immediately
./grafana_report.py --run-all-now             # generate every report once then exit
./grafana_report.py --daemon --api-port 9000  # run scheduler + FastAPI in one process
```
- Scheduler logs show `SCHEDULED`, `SENT`, or `ERROR` per cron run.
- Use `CTRL+C` to stop the scheduler gracefully.
- `--daemon` also boots the FastAPI server (defaults host `0.0.0.0`, port `8080`). Use `--api-host`/`--api-port` to override.

## 6. FastAPI Endpoint
Start the server:
```
uvicorn reporting.api:app --reload --port 8080
```
Download a PDF (API key required):
```
curl -L -H "X-API-Key: $API_KEY" \
  "http://localhost:8080/abc123?from=now-2d&to=now&timezone=Europe/Prague" \
  --output dashboard.pdf
```
Or with a Bearer token:
```
curl -L -H "Authorization: Bearer $API_KEY" \
  "http://localhost:8080/abc123?page_mode=paged&width=1920" \
  --output dashboard.pdf
```
- No emails are sent from API-triggered runs; callers receive the PDF directly.
- `/healthz` and `/readyz` are public. `/queue` and `/{uid}` require authentication.
- When `API_KEYS` is empty, protected endpoints return **503** (fail-closed).
- Invalid UIDs / time ranges / vars return **400**. A full queue returns **429**.

### Query parameters
| Param | Description |
|---|---|
| `from`, `to` | Time range (Grafana-style) |
| `timezone` | IANA timezone for header/banner. Grafana 13.2+ may send `browser` / `default` / `utc` — those map to `DEFAULT_TZ` (or UTC). Also accepts native `var-*` query params. |
| `vars` | `key1=value1,key2=value2` dashboard variables |
| `page_mode` | Override: `auto`, `a4`, `css`, `paged` |
| `width` | Viewport/PDF width in CSS px (320–7680) |
| `page_height_px` | Page height for `paged` mode |
| `theme` | Grafana theme (`light` / `dark`) |
| `scale_factor` | `device_scale_factor` for sharper canvases (0.5–4) |

## 7. Email Delivery Options
### Common switches
- `EMAIL_BACKEND=smtp` (default) or `sendgrid`.
- `REPORTS_EMAIL_ENABLED=true` to enable sending.

### SMTP env vars
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`, `SMTP_TLS_MODE`.

`SMTP_TLS_MODE` is one of `starttls` (default), `ssl` (SMTPS / port 465), or `none`.
If `SMTP_USER` is set, plaintext (failed STARTTLS / `none`) is refused.

### SendGrid env vars
`SENDGRID_API_KEY`, optional `SENDGRID_FROM`, `SENDGRID_REPLY_TO`.

### Template controls
| Variable | Description |
|---|---|
| `EMAIL_TEMPLATE_MODE` | `plain` (default) or `html`. `html` loads a shared template when a report omits `body_html`. |
| `EMAIL_TEMPLATE_HTML_PATH` | Path to a Jinja2 HTML file (used when `EMAIL_TEMPLATE_MODE=html`). |
| `EMAIL_TEMPLATE_IMAGES_DIR` | Directory of inline assets; filenames become content IDs (`cid:logo.png`). |

Per-report `email.body_html` overrides the shared template. Context provides `name`, `vars`, `period_*`, plus `text_body`/`html_body` for nesting. Templates run in a Jinja2 sandbox; HTML is auto-escaped.

## 8. PDF Rendering Controls
| Variable | Purpose |
|---|---|
| `PDF_SIZE_MODE` | `paged` (default), `auto`, `a4`, or `css`. |
| `PDF_PAGE_HEIGHT_PX` | Page height in CSS px for `paged` (0 = width / 1.414). |
| `PDF_DEVICE_SCALE_FACTOR` | Canvas sharpness (default `2`). |
| `PDF_THEME` | Appended as `theme=` to the dashboard URL (default `light`). |
| `PDF_MEDIA` | CSS media to emulate: `screen` (default) or `print`. |
| `PDF_MAX_VIEWPORT_HEIGHT` | Cap when expanding viewport to full dashboard height (default 30000). |
| `PDF_LANDSCAPE`, `PDF_PAPER_FORMAT` | Traditional print options for `a4` / `css` modes. |
| `WAIT_EXTRA_MS`, `READY_TIMEOUT_MS` | Control how long the renderer waits for panels. |
| `NETWORK_IDLE_TIMEOUT_MS` | Time (ms) to wait for network idle after load (default 20000). |
| `WAIT_BEFORE_READY_MS` | Optional delay (ms) before panel-ready checks (default 0). |
| `CHECK_QUERIES_TO_COMPLETE` | When `true`, wait for Grafana `/api/ds/query` requests to finish. |
| `PDF_FAST_MODE` | Skips heavy waits for small dashboards. Set to `0` for very slow dashboards. |
| `PDF_OUTDIR` | Directory where PDFs are written before emailing / download. |
| `QUEUE_MAX_PENDING` | Max queued jobs before API returns 429 (default 10). |
| `GRAFANA_TOKEN` | Service-account token; injected only for requests to the Grafana host. |
| `API_KEYS` | Comma-separated API keys for HTTP endpoints. |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, … |

### How `paged` mode works
1. Load the dashboard at the configured width.
2. Measure `.react-grid-layout` height (fallback: `gridPos` from dashboard JSON).
3. Expand the viewport to the full height so Grafana paints every panel.
4. Insert gaps between panels so page boundaries never bisect a panel (when possible).
5. Call `page.pdf({ width, height: pageHeight })` and let Chromium paginate.

`auto` produces a single page sized to content and falls back to `paged` when height would exceed `PDF_MAX_INCH`.

## 9. Troubleshooting
- **401 / 503 on export**: set `API_KEYS` and pass `Authorization: Bearer …` or `X-API-Key`.
- **Table headers missing**: ensure kiosk CSS is enabled (`FORCE_KIOSK_CSS=1`) and that the Grafana theme hasn't overridden table components.
- **Blank / clipped content**: keep `PDF_SIZE_MODE=paged` (or `auto`); the renderer expands the viewport to the full dashboard height, including nested Grafana row layouts.
- **Playwright login loops**: prefer `GRAFANA_TOKEN`; otherwise verify `GRAFANA_USER`/`GRAFANA_PASSWORD` and dashboard permissions.
- **Charts or tables empty in PDF**: set `CHECK_QUERIES_TO_COMPLETE=true`, increase `NETWORK_IDLE_TIMEOUT_MS`, or set `PDF_FAST_MODE=0`.
- **Email template issues**: confirm paths exist and HTML references `cid` that match filenames inside `EMAIL_TEMPLATE_IMAGES_DIR`.

## 10. Docker Image
1. Build the image (run inside the repo root):
  ```bash
  docker build -t grafana-reports:v1.1.0 .
  ```
2. Prepare an env file with Grafana/API/SMTP/SendGrid secrets (see `.env.example`).
3. Run the container; it defaults to `--daemon` (scheduler + FastAPI on 8080) as non-root `pwuser`:
  ```bash
  docker run -d --name grafana-reports \
    --env-file ./env.prod \
    -v $(pwd)/config.yml:/app/config.yml:ro \
    -p 8080:8080 \
    grafana-reports:v1.1.0
  ```
4. Override behaviour by appending CLI flags, for example:
  ```bash
  docker run --rm grafana-reports:v1.1.0 --run-once "Production VMS Monthly"
  ```

### Notes
- The image is based on `mcr.microsoft.com/playwright/python:v1.64.1-resolute` and installs Chromium during build.
- Playwright Python package is pinned to the same version as the base image (`1.64.1`).
- Mount a writable directory to `/tmp/pdfs` (or set `PDF_OUTDIR`) if you want to persist PDFs beyond email attachments / API downloads.
- Kubernetes manifests under `k8s/` include `securityContext`, `/dev/shm` emptyDir, and higher memory limits suitable for Chromium.

## 11. Tests
```
pip install -r requirements.txt
pytest -q
```
