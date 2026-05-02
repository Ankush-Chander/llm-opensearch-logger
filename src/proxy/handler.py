import json
import logging
import time
import uuid

import aiohttp
from aiohttp import web

from src.logging_pipeline.document import build_doc
from src.logging_pipeline.queue import enqueue_log
from src.proxy.response import stream_to_client, parse_response_content

log = logging.getLogger(__name__)


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    os_session: aiohttp.ClientSession = request.app["os_session"]
    ol_session: aiohttp.ClientSession = request.app["ol_session"]
    upstream_base: str = request.app["upstream_base"]
    service_name: str = request.app["service_name"]

    request_id = str(uuid.uuid4())
    ts_start = time.monotonic()

    # ---- Parse request ----
    req_raw = await request.read()
    req_body = None
    try:
        req_body = json.loads(req_raw) if req_raw else None
    except Exception as exc:
        log.warning("Could not parse request body: %s", exc)

    doc = build_doc(
        request_id=request_id,
        service_name=service_name,
        method=request.method,
        path=request.path,
        req_body=req_body,
    )

    # ---- Build upstream URL ----
    target_url = f"{upstream_base}{request.path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }

    # ---- Forward to upstream ----
    try:
        kwargs = {
            "method": request.method,
            "url": target_url,
            "headers": headers,
            "allow_redirects": False,
        }
        if req_raw:
            kwargs["data"] = req_raw

        async with ol_session.request(**kwargs) as upstream_resp:

            # ---- Prepare streaming response ----
            web_resp = web.StreamResponse(
                status=upstream_resp.status,
                headers={
                    k: v for k, v in upstream_resp.headers.items()
                    if k.lower() not in ("transfer-encoding", "content-length")
                },
            )
            web_resp.enable_chunked_encoding()
            await web_resp.prepare(request)

            # ---- Stream to client, collect for logging ----
            collected = await stream_to_client(upstream_resp, web_resp)
            await web_resp.write_eof()

            duration_ms = round((time.monotonic() - ts_start) * 1000, 2)

            # ---- Parse response for logging ----
            response_content, token_info = parse_response_content(collected)

            doc.update({
                "response_status":  upstream_resp.status,
                "response_content": response_content or None,
                "duration_ms":      duration_ms,
                **token_info,
            })

    except Exception as exc:
        log.exception("Proxy error for request_id=%s: %s", request_id, exc)
        doc["error"] = str(exc)
        enqueue_log(doc)
        return web.Response(status=500, text=str(exc))

    # ---- Enqueue log asynchronously ----
    enqueue_log(doc)
    return web_resp