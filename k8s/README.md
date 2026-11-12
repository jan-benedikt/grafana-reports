# Kubernetes Deployment

This folder contains sample manifests to run the Grafana PDF reporting service on Kubernetes. Customize the values, then apply them in the order shown below.

## Files
| File | Purpose |
| --- | --- |
| `configmap.yaml` | Bundles `config.yml` (report definitions). Adjust to match your dashboards.
| `secret-example.yaml` | Example secret with required environment variables (Grafana auth, SendGrid/SMTP). Copy and edit before applying.
| `deployment.yaml` | Runs one replica of the `ghcr.io/jan-benedikt/grafana-reports:latest` image in daemon mode (scheduler + FastAPI).
| `service.yaml` | ClusterIP service exposing the FastAPI endpoint on port `8080`.
| `ingress.yml` | Traefik IngressRoute for exposing the service via HTTPS (optional).

## Usage
1. Build and publish the Docker image (or replace `image:` with your registry/tag).
2. Create the namespace (all manifests use `grafana-reports` namespace):
   ```bash
   kubectl create namespace grafana-reports
   ```
3. Create supporting resources:
   ```bash
   kubectl apply -f k8s/configmap.yaml
   kubectl apply -f k8s/secret-example.yaml   # rename after editing secrets
   ```
4. Deploy and expose the service:
   ```bash
   kubectl apply -f k8s/deployment.yaml
   kubectl apply -f k8s/service.yaml
   ```
5. (Optional) Apply Traefik ingress for external access:
   ```bash
   kubectl apply -f k8s/ingress.yml   # adjust host and TLS settings first
   ```

## Customization Tips
- The HTTPS route strips the browser's `Cookie` header with `strip-report-cookies`: Grafana session cookies are not used by the exporter. Keep API-key authentication (or an independently configured trusted authentication proxy); stripping cookies must run after any middleware that needs them for authentication.
- For an existing installation in another namespace (for example `monitoring`), create `strip-report-cookies` in that namespace and attach it to the existing HTTPS route. This only changes requests forwarded to the exporter; it does not delete cookies from the browser or affect Grafana routes. If the connection is rejected before routing (an upstream proxy, TLS/HTTP parser, or header-size limit), this middleware cannot repair it: compare direct application and direct Traefik requests and inspect ingress/upstream logs.
- **Keep `replicas: 1`**: daemon mode runs the cron scheduler in-process, so extra replicas send duplicate report emails.
- The `ServersTransport` in `ingress.yml` raises Traefik's backend `responseHeaderTimeout` to 600s — PDF generation holds the request open until the file is ready. Any other proxy/LB in front must allow equally long response waits.
- Do **not** inject `X-API-Key` via a Traefik `headers` middleware: it exposes the export endpoint unauthenticated to anyone reaching the hostname and places the key in a non-secret manifest.
- Mount a PersistentVolume and set `PDF_OUTDIR` if you need PDFs persisted beyond email delivery.
- Replace `SENDGRID_*` variables with SMTP equivalents if desired.
- Update probes or resource limits to fit your cluster policy; keep probe `timeoutSeconds` ≥ 5 — Chromium can saturate the CPU limit during large renders.
- To run the FastAPI server on another port, change `--api-port` in `deployment.yaml` and adjust the Service.

## Diagnosing failures
- Container restarts: `kubectl -n grafana-reports get pod -o jsonpath='{..lastState.terminated}'` — `OOMKilled` means the memory limit was hit mid-render (lower `PDF_MAX_RENDER_PIXELS`/`PDF_DEVICE_SCALE_FACTOR` or raise the limit); exit 137 with liveness events means probe timeouts.
- Downloads failing while the server logs `PDF generated`: compare the Traefik access-log status for the request — `504` points to a proxy response timeout, `200` with a browser-side empty reply points to an entrypoint `writeTimeout` or an upstream hop.
