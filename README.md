# SmartLLM-proxy

LiteLLM proxy running on k3s, routing [OpenClaw](https://github.com/FlatMoon-Eva/OpenClaw) requests across multiple Gemini API keys.

Part of the **OpenClaw Task Router** system:
```
OpenClaw → ClawRouter → SmartLLM-proxy → Gemini API
```

Monitoring is handled by [Tineye](https://github.com/FlatMoon-Eva/Tineye) (side-channel).

## Architecture

```
OpenClaw (namespace: openclaw)
    │
    ▼
LiteLLM Proxy  :4000  (namespace: smartllm)
    │  simple-brain          → gemini-2.5-flash        (rpm 10, multi-key shuffle)
    │  simple-brain-fallback → gemini-3-flash-preview
    │  smart-brain           → gemini-2.5-pro           (rpm 5)
    │
    ▼
Gemini API (Google AI Studio)
```

**Fallback chains:**
- `simple-brain` → `simple-brain-fallback` → fail
- `smart-brain` → `simple-brain` → fail

## Files

| File | Description |
|------|-------------|
| `middleware.py` | FastAPI middleware — global rate limiter |
| `litellm_config.yaml.example` | ConfigMap template (no real keys) |
| `start.sh` | Local dev start script |
| `k8s/` | Kubernetes manifests |

## k8s Setup

### Prerequisites

- k3s cluster
- `kubectl` access
- Gemini API keys from [aistudio.google.com](https://aistudio.google.com)

### 1. Create namespace and secrets

```bash
kubectl apply -f k8s/namespace.yaml

# Postgres password
kubectl create secret generic postgres-secret -n smartllm \
  --from-literal=POSTGRES_PASSWORD=<your-password>
```

### 2. Apply ConfigMap

```bash
cp litellm_config.yaml.example litellm_config.yaml
# Fill in your Gemini API keys

kubectl create configmap litellm-config -n smartllm \
  --from-file=config.yaml=litellm_config.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 3. Deploy

```bash
kubectl apply -f k8s/
```

### 4. Update OpenClaw config

```json
{
  "baseUrl": "http://litellm.smartllm.svc.cluster.local:4000/v1"
}
```

## Updating the model config

```bash
kubectl get configmap litellm-config -n smartllm \
  -o jsonpath='{.data.config\.yaml}' > litellm_config.yaml

# edit litellm_config.yaml ...

kubectl create configmap litellm-config -n smartllm \
  --from-file=config.yaml=litellm_config.yaml \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/litellm -n smartllm
```

## Quota Notes

- Free tier quota resets daily at **UTC 00:00**
- All keys under the same Google Cloud project share the same daily quota

