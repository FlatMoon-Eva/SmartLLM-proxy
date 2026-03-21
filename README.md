# litellm-proxy

LiteLLM proxy running on k3s, routing [OpenClaw](https://github.com/openclaw) requests across multiple Gemini API keys with a monitoring dashboard.

## Architecture

```
OpenClaw (k8s pod)
    │
    ▼
LiteLLM Proxy  :4000  (k8s Deployment, namespace: litellm)
    │  simple-brain  → gemini-2.5-flash  (simple-shuffle, rpm 10)
    │  simple-brain-fallback → gemini-3-flash-preview (auto-fallback)
    │  smart-brain   → gemini-2.5-pro    (rpm 5)
    │
    ▼
Gemini API (Google AI Studio free tier)

Dashboard  :8888  (systemd, host)
Collector  :8889  (systemd, host)  ← receives POST from LiteLLM logger
```

**Fallback chain:** `simple-brain` → `simple-brain-fallback` → (fail)  
**Fallback chain:** `smart-brain` → `simple-brain` → (fail)

## Files

| File | Description |
|------|-------------|
| `dashboard.py` | Monitoring dashboard (port 8888) |
| `collector.py` | Log collector — receives POST from LiteLLM custom logger, writes `requests.jsonl` |
| `middleware.py` | FastAPI middleware for LiteLLM — global rate limiter |
| `litellm_config.yaml.example` | ConfigMap template (no real keys) |
| `start.sh` | Local dev start script (not used in k8s) |

## Setup

### Prerequisites

- k3s cluster with `litellm` namespace
- `sudo kubectl` access from host
- Python 3.10+
- Gemini API keys from [aistudio.google.com](https://aistudio.google.com)

### 1. Kubernetes — LiteLLM Proxy

Copy the config example, fill in your API keys, then apply:

```bash
cp litellm_config.yaml.example litellm_config.yaml
# edit litellm_config.yaml — replace YOUR_GEMINI_API_KEY_* with real keys

sudo kubectl create configmap litellm-config -n litellm \
  --from-file=config.yaml=litellm_config.yaml \
  --dry-run=client -o yaml | sudo kubectl apply -f -

sudo kubectl rollout restart deployment/litellm -n litellm
```

Apply the LiteLLM deployment (see `k8s/` directory):

```bash
sudo kubectl apply -f k8s/
```

### 2. Dashboard & Collector — systemd

Set credentials (never commit this file):

```bash
sudo mkdir -p /etc/litellm-dashboard
sudo tee /etc/litellm-dashboard/env <<EOF
DASHBOARD_USER=your@email.com
DASHBOARD_PASS=your_password_here
EOF
sudo chmod 600 /etc/litellm-dashboard/env
```

Install and start services:

```bash
sudo cp systemd/litellm-dashboard.service /etc/systemd/system/
sudo cp systemd/litellm-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now litellm-dashboard litellm-collector
```

### 3. chart.js (local copy)

The dashboard serves chart.js locally to avoid CDN blocking:

```bash
curl -L https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js \
  -o /home/ubuntu/litellm-proxy/chart.min.js
```

### 4. OpenClaw Config

In `openclaw.json`:

```json
{
  "providers": {
    "litellm-gemini": {
      "baseUrl": "http://litellm.litellm.svc.cluster.local:4000/v1",
      "apiKey": "any"
    }
  },
  "agentModel": "litellm-gemini/simple-brain"
}
```

## Dashboard

- URL: `http://<server-ip>:8888`
- Login: email + password from `/etc/litellm-dashboard/env`
- Session cookie valid 30 days

Features:
- Request count chart (1h / 1d / 2w / 1mo / 1y)
- Per-key status: `ok` / `rate_limit` / `out_of_quota` / `invalid`
- Model switcher per brain group (updates ConfigMap + restarts deployment)
- Auto-refresh every 30s

## Routing & Retry Logic

Strategy: `simple-shuffle` — each request picks a key at random (not round-robin).

**On failure (429 / 5xx):**

| Setting | Value | Meaning |
|---------|-------|---------|
| `allowed_fails` | 1 | 1 failure puts a key into cooldown |
| `cooldown_time` | 60s | Cooled-down keys are skipped for 60s |
| `num_retries` | 6 | Same request retries up to 6 times with different keys |
| `retry_after` | 5s | Wait 5s between retries |

**Fallback chain** (triggered after all retries exhausted):
```
simple-brain → simple-brain-fallback
smart-brain  → simple-brain
```

**Concurrency note:** Free-tier quota is per Google Cloud project, not per key.
Multiple keys from the same project share the same RPM/RPD limit.
True parallel capacity only increases if keys come from different projects.

## Updating ConfigMap

```bash
sudo kubectl get configmap litellm-config -n litellm \
  -o jsonpath='{.data.config\.yaml}' > litellm_config.yaml

# edit litellm_config.yaml ...

sudo kubectl create configmap litellm-config -n litellm \
  --from-file=config.yaml=litellm_config.yaml \
  --dry-run=client -o yaml | sudo kubectl apply -f -

sudo kubectl rollout restart deployment/litellm -n litellm
```

## Quota Notes

- Free tier quota resets daily at **UTC 00:00** (08:00 Taiwan time)
- All keys under the same Google Cloud project share the same per-model daily quota
- If all keys show `out_of_quota`, switching models or waiting for reset are the only options
