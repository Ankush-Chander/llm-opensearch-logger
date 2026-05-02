import json
import logging

import aiohttp
from aiohttp import web

from src.config import LOG_BODY_MAX_BYTES

log = logging.getLogger(__name__)


from src.proxy.parsers.base import BaseParser

async def stream_to_client(
    upstream_resp: aiohttp.ClientResponse,
    web_resp: web.StreamResponse,
    parser: BaseParser = None,
) -> bytes:
    """Stream upstream response to client, optionally feeding a parser."""
    collected = bytearray()
    accumulated = 0

    async for chunk in upstream_resp.content.iter_any():
        await web_resp.write(chunk)
        
        # Feed parser in real-time
        if parser:
            try:
                parser.consume(chunk)
            except Exception as exc:
                log.warning("Parser error: %s", exc)

        if accumulated < LOG_BODY_MAX_BYTES:
            collected.extend(chunk)
            accumulated += len(chunk)
            
    return bytes(collected)