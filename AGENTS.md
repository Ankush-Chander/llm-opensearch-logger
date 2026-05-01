# AGENTS.md

## Project Overview
LLM reverse proxy that logs all traffic to OpenSearch. Listens on configurable ports, forwards requests to upstream LLM services (Ollama, OpenAI-compatible), and asynchronously indexes request/response pairs.

## Architecture
- **Entry**: `__main__.py` → `src.server.run()` or `ollama_proxy.py` → `src.proxy.server.run()`
- **Proxy**: `src.proxy.server.py` spawns one aiohttp `AppRunner` per mapping, each with `proxy_handler` as catch-all route
- **Handler**: `src.proxy.handler.py` forwards to upstream, streams response to client, collects bytes for logging
- **Response Parsing**: `src.proxy.response.py` handles OpenAI SSE (`data: {...}`) and Ollama NDJSON formats
- **Logging Pipeline**: Async queue (`src.logging_pipeline.queue.py`) → `log_worker` → `index_document`
- **Config**: `src.config.py` reads env vars, supports multi-proxy mappings via `PROXY_MAPPINGS`

## Key Files
| File | Purpose |
|------|---------|
| `src/proxy/handler.py` | Main request handler, upstream forwarding, document assembly |
| `src/proxy/response.py` | Stream collection, SSE/NDJSON parsing, token extraction |
| `src/logging_pipeline/document.py` | Document builder, conversation ID hashing, turn numbering |
| `src/logging_pipeline/queue.py` | Async queue (maxsize 1000), drop-on-full policy |
| `src/opensearch/client.py` | Index creation, document indexing |
| `src/opensearch/schema.py` | OpenSearch mapping definition |
| `src/config.py` | Env var config, mapping parser |

## Environment Variables
- `PROXY_HOST` — Bind address (default: `0.0.0.0`)
- `PROXY_PORT` — Listen port (default: `11434`)
- `OLLAMA_HOST` / `OLLAMA_PORT` — Upstream host/port (default: `127.0.0.1:11435`)
- `OPENSEARCH_URL` — OpenSearch endpoint (default: `http://localhost:9200`)
- `OPENSEARCH_INDEX` — Index name (default: `ollama-traffic`)
- `LOG_BODY_MAX_BYTES` — Max response bytes to log (default: 10MB)
- `PROXY_MAPPINGS` — Comma-separated mappings: `port:upstream_port:name`

## Running
```bash
python -m .          # via __main__.py
python ollama_proxy.py  # standalone entry
```

## Dependencies
- `aiohttp>=3.9.0` — Async HTTP server & client
- `fire==0.7.1` — CLI (used by `src/eval.py`)

## Conventions
- All I/O is async; no blocking calls in request path
- Logging is fire-and-forget via queue; full queue drops documents
- Conversation ID = SHA1(system_prompt | first_user_msg | model)
- Document `_id` = `conversation_id` (overwrites previous turns)
- No comments in code unless explicitly requested
