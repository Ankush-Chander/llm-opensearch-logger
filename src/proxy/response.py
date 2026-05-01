import json
import logging

import aiohttp
from aiohttp import web

from src.config import LOG_BODY_MAX_BYTES

log = logging.getLogger(__name__)


async def stream_to_client(
    upstream_resp: aiohttp.ClientResponse,
    web_resp: web.StreamResponse,
) -> bytes:
    """Stream upstream response to client, collecting bytes for logging."""
    collected = bytearray()
    accumulated = 0

    async for chunk in upstream_resp.content.iter_any():
        await web_resp.write(chunk)
        if accumulated < LOG_BODY_MAX_BYTES:
            collected.extend(chunk)
            accumulated += len(chunk)
        else:
            log.warning(f"Skipping logging of {chunk} as it exceeds the log body max size of {LOG_BODY_MAX_BYTES} bytes")
            continue
            
    return bytes(collected)

def parse_response_content(raw: bytes) -> tuple[str, dict]:
    print(f" raw response: {raw}")
    content_parts = []
    token_info = {}

    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue

        # ---- OpenAI-style SSE: "data: {...}" ----
        if line.startswith(b"data:"):
            payload = line[5:].strip()
            if payload == b"[DONE]":
                continue
            try:
                chunk_json = json.loads(payload)
            except Exception as exc:
                log.debug("Could not parse SSE payload: %s", exc)
                continue

            for choice in chunk_json.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content") is not None:
                    content_parts.append(delta["content"])

            if "usage" in chunk_json:
                token_info = chunk_json["usage"]

        # ---- Ollama native NDJSON: plain JSON per line ----
        else:
            try:
                chunk_json = json.loads(line)
            except Exception:
                continue

            # /api/chat format
            message = chunk_json.get("message", {})
            if message.get("content"):
                content_parts.append(message["content"])

            # /api/generate format
            if chunk_json.get("response"):
                content_parts.append(chunk_json["response"])

            # Token counts (present on the final chunk where done=true)
            if chunk_json.get("done"):
                if "prompt_eval_count" in chunk_json:
                    token_info = {
                        "prompt_tokens":     chunk_json.get("prompt_eval_count"),
                        "completion_tokens": chunk_json.get("eval_count"),
                        "total_tokens": (
                            chunk_json.get("prompt_eval_count", 0) +
                            chunk_json.get("eval_count", 0)
                        ),
                    }

    return "".join(content_parts), token_info