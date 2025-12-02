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
- Mount a PersistentVolume and set `PDF_OUTDIR` if you need PDFs persisted beyond email delivery.
- Replace `SENDGRID_*` variables with SMTP equivalents if desired.
- Update probes or resource limits to fit your cluster policy.
- To run the FastAPI server on another port, change `--api-port` in `deployment.yaml` and adjust the Service.
