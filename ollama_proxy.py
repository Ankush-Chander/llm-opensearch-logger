#!/usr/bin/env python3
"""
Ollama logging reverse proxy.

Listens on PROXY_PORT (default 11434) and forwards all traffic to
Ollama running on OLLAMA_PORT (default 11435). Every request/response
pair is indexed asynchronously into OpenSearch.

Environment variables:
  PROXY_HOST         Host to bind (default: 0.0.0.0)
  PROXY_PORT         Port to listen on (default: 11434)
  OLLAMA_HOST        Ollama host (default: 127.0.0.1)
  OLLAMA_PORT        Ollama port after rebind (default: 11435)
  OPENSEARCH_URL     OpenSearch base URL (default: http://localhost:9200)
  OPENSEARCH_INDEX   Index name (default: ollama-traffic)
  LOG_BODY_MAX_BYTES Max bytes of body logged (default: 65536)
"""

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from aiohttp import web

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_HOST = os.getenv("PROXY_HOST", "0.0.0.0")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200").rstrip("/")
OPENSEARCH_INDEX = os.getenv("OPENSEARCH_INDEX", "ollama-traffic")
LOG_BODY_MAX_BYTES = int(os.getenv("LOG_BODY_MAX_BYTES", str(64 * 1024)))

# Parse mappings: "listen_port:upstream_port[:name]" or "listen_port:host:upstream_port[:name]"
# Example: "11434:11435:ollama,8002:8003:llama-server"
PROXY_MAPPINGS_RAW = os.getenv("PROXY_MAPPINGS", "")
MAPPINGS = []

if PROXY_MAPPINGS_RAW:
    for item in PROXY_MAPPINGS_RAW.split(","):
        item = item.strip().strip("'\"")
        parts = item.split(":")
        if len(parts) == 2:
            MAPPINGS.append({"port": int(parts[0]), "upstream": f"http://127.0.0.1:{parts[1]}", "name": "default"})
        elif len(parts) == 3:
            # Could be listen:host:port OR listen:port:name
            if parts[1].isdigit():
                MAPPINGS.append({"port": int(parts[0]), "upstream": f"http://127.0.0.1:{parts[1]}", "name": parts[2]})
            else:
                MAPPINGS.append({"port": int(parts[0]), "upstream": f"http://{parts[1]}:{parts[2]}", "name": "default"})
        elif len(parts) == 4:
            MAPPINGS.append({"port": int(parts[0]), "upstream": f"http://{parts[1]}:{parts[2]}", "name": parts[3]})
else:
    # Fallback to old environment variables
    OLD_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "127.0.0.1")
    OLD_OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11435"))
    OLD_PROXY_PORT = int(os.getenv("PROXY_PORT", "11434"))
    MAPPINGS.append({"port": OLD_PROXY_PORT, "upstream": f"http://{OLD_OLLAMA_HOST}:{OLD_OLLAMA_PORT}", "name": "ollama"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ollama-proxy")

# ---------------------------------------------------------------------------
# OpenSearch helpers
# ---------------------------------------------------------------------------

async def ensure_index(session: aiohttp.ClientSession) -> None:
    """Create the index with a mapping if it does not already exist."""
    mapping = {
        "mappings": {
            "properties": {
                "request_id":      {"type": "keyword"},
                "service":         {"type": "keyword"},
                "conversation_id": {"type": "keyword"},
                "turn_number":     {"type": "integer"},
                "timestamp":       {"type": "date"},
                "method":          {"type": "keyword"},
                "path":            {"type": "keyword"},
                "query_string":    {"type": "keyword"},
                "request_headers": {"type": "object",  "enabled": False},
                "request_body":    {"type": "object",  "enabled": True},
                "response_status": {"type": "integer"},
                "response_content":{"type": "text"},
                "response_reasoning":{"type": "text",  "index": False},
                "duration_ms":     {"type": "float"},
                "model":           {"type": "keyword"},
                "prompt_tokens":   {"type": "integer"},
                "completion_tokens":{"type": "integer"},
                "total_tokens":    {"type": "integer"},
                "truncated":       {"type": "boolean"},
                "error":           {"type": "text"},
            }
        },
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0
        }
    }
    url = f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}"
    async with session.put(url, json=mapping, headers={"Content-Type": "application/json"}) as resp:
        body = await resp.json()
        if resp.status in (200, 400):  # 400 = already exists
            acknowledged = body.get("acknowledged", body.get("error", {}).get("type") == "resource_already_exists_exception")
            log.info("Index ready: %s (status=%d)", OPENSEARCH_INDEX, resp.status)
        else:
            log.warning("Unexpected index creation response: %d %s", resp.status, body)


async def index_document(session: aiohttp.ClientSession, doc: dict) -> None:
    """Replace the conversation document in OpenSearch using conversation_id as _id."""
    doc_id = doc.get("conversation_id") or doc["request_id"]
    url = f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_doc/{doc_id}"
    try:
        async with session.put(url, json=doc, headers={"Content-Type": "application/json"}) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                log.warning("OpenSearch indexing failed (%d): %s", resp.status, body[:200])
    except Exception as exc:
        log.warning("Failed to index document: %s", exc)

# ---------------------------------------------------------------------------
# Body parsing helpers
# ---------------------------------------------------------------------------

def _truncate(raw: bytes) -> tuple[bytes, bool]:
    if len(raw) > LOG_BODY_MAX_BYTES:
        return raw[:LOG_BODY_MAX_BYTES], True
    return raw, False


def _try_json(raw: bytes) -> Optional[dict]:
    """Try to parse raw bytes as JSON, return None on failure."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _extract_conversation_id(body: Optional[dict]) -> tuple[str, int]:
    """
    Derive a stable conversation_id and turn_number from the messages array.

    conversation_id = SHA-1( system_prompt + "|" + first_user_message + "|" + model )
    This is stable across all turns of the same conversation.

    turn_number = number of user messages in the array (1-indexed).
    """
    if not body:
        return str(uuid.uuid4()), 1

    messages: list = body.get("messages") or []
    model: str = body.get("model", "")

    system_prompt = ""
    first_user_msg = ""
    user_turn_count = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if role == "system" and not system_prompt:
            system_prompt = content
        elif role == "user":
            user_turn_count += 1
            if not first_user_msg:
                first_user_msg = content

    if not first_user_msg:
        return str(uuid.uuid4()), 1

    key = f"{system_prompt}|{first_user_msg}|{model}"
    conversation_id = hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()
    return conversation_id, max(user_turn_count, 1)


def _extract_token_counts(body: Optional[dict]) -> dict:
    """Pull token usage out of an Ollama response body (if present)."""
    if not body:
        return {}
    result = {}
    # /api/generate format
    if "prompt_eval_count" in body:
        result["prompt_tokens"] = body.get("prompt_eval_count")
    if "eval_count" in body:
        result["completion_tokens"] = body.get("eval_count")
    # /api/chat format
    usage = body.get("usage") or {}
    if "prompt_tokens" in usage:
        result["prompt_tokens"] = usage["prompt_tokens"]
    if "completion_tokens" in usage:
        result["completion_tokens"] = usage["completion_tokens"]
    if "prompt_tokens" in result and "completion_tokens" in result:
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    return result

# ---------------------------------------------------------------------------
# Streaming response collector
# ---------------------------------------------------------------------------

async def _collect_streaming_body(upstream_resp: aiohttp.ClientResponse, web_resp: web.StreamResponse) -> bytes:
    """
    Stream body bytes to the client while collecting them for logging.
    Handles both NDJSON streams (Ollama default) and plain responses.
    """
    collected = bytearray()
    async for chunk in upstream_resp.content.iter_any():
        collected.extend(chunk)
        await web_resp.write(chunk)
    return bytes(collected)

# ---------------------------------------------------------------------------
# Main proxy handler
# ---------------------------------------------------------------------------

async def proxy_handler(request: web.Request) -> web.StreamResponse:
    app = request.app
    os_session: aiohttp.ClientSession = app["os_session"]
    ol_session: aiohttp.ClientSession = app["ol_session"]
    upstream_base: str = app["upstream_base"]
    service_name: str = app["service_name"]

    request_id = str(uuid.uuid4())
    ts_start = time.monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()

    # --- Read request body ---
    req_raw = await request.read()
    req_raw_trunc, req_truncated = _truncate(req_raw)
    req_body = _try_json(req_raw)
    model = (req_body or {}).get("model", "")
    conversation_id, turn_number = _extract_conversation_id(req_body)

    # --- Build upstream URL ---
    qs = request.query_string
    target_url = f"{upstream_base}{request.path}"
    if qs:
        target_url += f"?{qs}"

    # Forward all original headers except Host
    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "content-length")}

    # Prefer the client's own stable session ID over our derived hash
    session_affinity = request.headers.get("x-session-affinity")
    if session_affinity:
        conversation_id = session_affinity

    # Keep only signal-bearing request headers — drop boilerplate
    _KEEP_REQ_HEADERS = {"user-agent", "x-session-affinity", "x-request-id"}
    useful_headers = {k: v for k, v in request.headers.items()
                      if k.lower() in _KEEP_REQ_HEADERS}

    doc: dict = {
        "request_id": request_id,
        "service": service_name,
        "conversation_id": conversation_id,
        "turn_number": turn_number,
        "timestamp": timestamp,
        "method": request.method,
        "path": request.path,
        "query_string": qs,
        "request_headers": useful_headers,
        "request_body": req_body,
        "model": model,
        "truncated": req_truncated,
    }

    log.info("[%s] %s %s model=%s body=%dB",
             request_id[:8], request.method, request.path, model, len(req_raw))

    try:
        async with ol_session.request(
            request.method,
            target_url,
            headers=fwd_headers,
            data=req_raw,
            allow_redirects=False,
        ) as upstream_resp:

            # Prepare client-facing streaming response
            web_resp = web.StreamResponse(
                status=upstream_resp.status,
                headers={k: v for k, v in upstream_resp.headers.items()
                         if k.lower() not in ("transfer-encoding", "content-length")},
            )
            await web_resp.prepare(request)

            # Stream body back to client and collect for logging
            resp_raw = await _collect_streaming_body(upstream_resp, web_resp)
            await web_resp.write_eof()

            duration_ms = (time.monotonic() - ts_start) * 1000
            _, resp_truncated = _truncate(resp_raw)

            # Parse SSE stream: each line is "data: {...}" or "data: [DONE]"
            # Collect final answer, reasoning, and token usage from the chunks.
            resp_content = []
            resp_reasoning = []
            resp_body: Optional[dict] = None

            for raw_line in resp_raw.split(b"\n"):
                raw_line = raw_line.strip()
                if not raw_line.startswith(b"data:"):
                    continue
                payload = raw_line[5:].strip()
                if payload == b"[DONE]":
                    continue
                chunk = _try_json(payload)
                if not chunk:
                    continue
                # Last non-[DONE] chunk with usage stats
                if chunk.get("usage"):
                    resp_body = chunk
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        resp_content.append(delta["content"])
                    if delta.get("reasoning"):
                        resp_reasoning.append(delta["reasoning"])

            token_info = _extract_token_counts(resp_body)

            doc.update({
                "response_status": upstream_resp.status,
                "response_content": "".join(resp_content) or None,
                "response_reasoning": "".join(resp_reasoning) or None,
                "duration_ms": round(duration_ms, 2),
                "truncated": req_truncated or resp_truncated,
                **token_info,
            })

            log.info("[%s] → %d  %.0fms  tokens=%s",
                     request_id[:8], upstream_resp.status, duration_ms,
                     token_info.get("total_tokens", "?"))

    except aiohttp.ClientConnectorError as exc:
        duration_ms = (time.monotonic() - ts_start) * 1000
        doc.update({"error": str(exc), "duration_ms": round(duration_ms, 2)})
        log.error("[%s] Upstream connection failed: %s", request_id[:8], exc)
        asyncio.ensure_future(index_document(os_session, doc))
        return web.Response(status=502, text=f"Ollama unreachable: {exc}")

    except Exception as exc:
        duration_ms = (time.monotonic() - ts_start) * 1000
        doc.update({"error": str(exc), "duration_ms": round(duration_ms, 2)})
        log.exception("[%s] Unexpected error", request_id[:8])
        asyncio.ensure_future(index_document(os_session, doc))
        return web.Response(status=500, text="Proxy error")

    # Async log — don't block the response
    asyncio.ensure_future(index_document(os_session, doc))
    return web_resp

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def run_multi_proxy():
    # Session for upstreams
    ol_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=10)
    )
    # Session for OpenSearch
    os_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10)
    )

    # Wait for OpenSearch
    for attempt in range(20):
        try:
            await ensure_index(os_session)
            break
        except Exception as exc:
            log.warning("OpenSearch not ready (attempt %d/20): %s", attempt + 1, exc)
            await asyncio.sleep(3)

    runners = []
    for m in MAPPINGS:
        app = web.Application()
        app["ol_session"] = ol_session
        app["os_session"] = os_session
        app["upstream_base"] = m["upstream"]
        app["service_name"] = m["name"]
        app.router.add_route("*", "/{path_info:.*}", proxy_handler)
        
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, PROXY_HOST, m["port"])
        await site.start()
        runners.append(runner)
        log.info("Started proxy for '%s' on %s:%d → %s", m["name"], PROXY_HOST, m["port"], m["upstream"])

    try:
        # Keep running forever
        while True:
            await asyncio.sleep(3600)
    finally:
        for r in runners:
            await r.cleanup()
        await ol_session.close()
        await os_session.close()


if __name__ == "__main__":
    log.info("Starting generalized logging proxy")
    log.info("OpenSearch: %s  index: %s", OPENSEARCH_URL, OPENSEARCH_INDEX)
    try:
        asyncio.run(run_multi_proxy())
    except KeyboardInterrupt:
        pass