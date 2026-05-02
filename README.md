# Ollama → OpenSearch traffic logger

Captures every request and response to Ollama and indexes them into
OpenSearch, with OpenSearch Dashboards for visualization.

---

## Architecture

```
Client :11434
  → ollama-proxy (Python / aiohttp)
      → Ollama   :11435     (forwarded, streamed back)
      → OpenSearch :9200    (async log write)

OpenSearch Dashboards :5601  (browser UI)
```

---

## 1. Reconfigure Ollama to listen on a different port

Edit the Ollama systemd override so it binds to `0.0.0.0:11435`
instead of the default `0.0.0.0:11434`:

```bash
sudo systemctl edit ollama
```

Add (or merge into the existing override):

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11435"
```

Reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama

# Verify
curl http://0.0.0.0:11435/api/tags
```

---

## 2. Start OpenSearch and Dashboards (Docker)

If you have Docker installed, you can run everything (OpenSearch, Dashboards, and the Proxy) using the provided `docker-compose.yml`.

```bash
# Build and start all services
docker compose up -d --build

# Verify the proxy is running
docker compose logs -f proxy
```

The proxy will be available at `http://localhost:11434`. By default, it expects Ollama to be running on the host at port `11435`.

OpenSearch Dashboards → http://localhost:5601

---

## 3. Install the proxy (Manual / Local Dev)

```bash
# clone the repo
git clone <repo_url>
cd llm-opensearch-logger
# create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# install dependencies
pip install -r requirements.txt
```

## 4. Run the proxy
```bash
# Run the proxy (simple test)
python3 llm_proxy.py


```

---

## 4. Install the systemd service

```bash
# Install as a user service (no sudo needed)
cp llm-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now llm-proxy

# Confirm it is running
systemctl --user status llm-proxy
journalctl --user -u llm-proxy -f
```

---

## 5. Verify end-to-end

```bash
# Send a request through the proxy (same port clients already use)
curl http://localhost:11434/api/tags

# Check documents are landing in OpenSearch
curl 'http://localhost:9200/ollama-traffic/_count?pretty'
curl 'http://localhost:9200/ollama-traffic/_search?pretty&size=1'
```

---

## 6. OpenSearch Dashboards — create an index pattern

1. Open http://localhost:5601
2. Go to **Management → Stack Management → Index Patterns**
3. Create pattern: `ollama-traffic*`  (time field: `timestamp`)
4. Go to **Discover** to explore logs.

### Useful saved searches / visualizations to create

| Visualization | Metric | Split by |
|---|---|---|
| Request rate | Count | Date histogram on `timestamp` |
| Model usage | Count | Terms on `model` |
| Avg duration | Avg `duration_ms` | Date histogram |
| Token usage | Sum `total_tokens` | Terms on `model` |
| Avg turn number | Avg `turn_number` | Terms on `model` |
| Response length | Avg keyword length on `response_content` | Date histogram |
| Error rate | Count where `error` exists | Date histogram |

---

## Configuration reference

All settings can be overridden via environment variables in the
`[Service]` section of the systemd unit.

| Variable | Default | Description |
|---|---|---|
| `PROXY_HOST` | `0.0.0.0` | Address the proxy binds to |
| `PROXY_PORT` | `11434` | Port the proxy listens on (ignored if `PROXY_MAPPINGS` is set) |
| `OLLAMA_HOST` | `0.0.0.0` | Ollama host (ignored if `PROXY_MAPPINGS` is set) |
| `OLLAMA_PORT` | `11435` | Ollama port (ignored if `PROXY_MAPPINGS` is set) |
| `PROXY_MAPPINGS`| `""` | Comma-separated mappings: `listen:upstream[:name]` |
| `OPENSEARCH_URL` | `http://localhost:9200` | OpenSearch endpoint |
| `OPENSEARCH_INDEX` | `ollama-traffic` | Index name |
| `LOG_BODY_MAX_BYTES` | `65536` (64 KB) | Max body bytes kept before truncation |

### Multiple Backends (Generalization)

To proxy and log multiple services (e.g., Ollama and llama-server), use `PROXY_MAPPINGS`:

```ini
Environment=PROXY_MAPPINGS=11434:11435:ollama,8002:8003:llama-server
```

Each mapping follows the format `listen_port:upstream_port[:service_name]` or `listen_port:host:upstream_port[:service_name]`. The `service_name` will be stored in the `service` field in OpenSearch.

**Note for Docker users:** Use `host.docker.internal` as the host to reach services running on your local machine:
```yaml
environment:
  - PROXY_MAPPINGS=11434:host.docker.internal:11435:ollama,8002:host.docker.internal:8003:llama-server
```
Don't forget to also add these ports to the `ports:` section of your `docker-compose.yml`.

---

## Conversation tracking

The proxy derives a stable `conversation_id` from the request body:

```
conversation_id = SHA-1( system_prompt + "|" + first_user_message + "|" + model )
```

This hash is used as the OpenSearch document `_id`, so each new turn
**overwrites** the previous document for the same conversation. The
final stored document reflects the last turn's response content, reasoning,
token counts, and turn number.

If the client sends an `X-Session-Affinity` header, its value is used
directly as `conversation_id` instead of the derived hash. This lets the
client control grouping.

Only a small set of request headers (`user-agent`, `x-session-affinity`,
`x-request-id`) are stored. `response_reasoning` is mapped with
`"index": false` so it is stored but not searchable — useful for audit
without bloating the index.

---

## What is logged per request

The proxy uses `conversation_id` as the OpenSearch document `_id`, so each
new turn from the same conversation overwrites the previous one.  The final
document contains the last known state — response content, reasoning, token
counts, and the final turn number.

```json
{
  "request_id":         "uuid",
  "service":            "ollama",
  "conversation_id":    "sha1(system|first_user|model)",
  "turn_number":        3,
  "timestamp":          "2025-04-25T10:00:00Z",
  "method":             "POST",
  "path":               "/api/chat",
  "query_string":       "",
  "request_headers":    { "user-agent": "...", "x-session-affinity": "..." },
  "request_body":       { "model": "llama3", "messages": [...] },
  "model":              "llama3",
  "response_status":    200,
  "response_content":   "<assistant answer text>",
  "response_reasoning": "<chain-of-thought reasoning (unindexed)>",
  "duration_ms":        1234.56,
  "prompt_tokens":      42,
  "completion_tokens":  118,
  "total_tokens":       160,
  "truncated":          false,
  "error":              null
}
```

---

## Troubleshooting

**Proxy starts but Ollama is unreachable**
```bash
# Check Ollama is bound to the new port
ss -tlnp | grep 11435
curl http://127.0.0.1:11435/api/tags
```

**Documents not appearing in OpenSearch**
```bash
# Check proxy logs
journalctl --user -u ollama-proxy --since "5 min ago"

# Check OpenSearch is up
curl http://localhost:9200/_cat/indices?v
```

**Port 11434 conflict after restarting Ollama**
Make sure the `OLLAMA_HOST` environment variable is set in the
Ollama override and that `systemctl daemon-reload` was run.
