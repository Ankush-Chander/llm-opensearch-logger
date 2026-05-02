import asyncio
import logging

import aiohttp
from aiohttp import web

from src.config import PROXY_HOST, MAPPINGS
from src.opensearch.client import ensure_index
from src.logging_pipeline.queue import log_worker
from src.proxy.handler import proxy_handler

log = logging.getLogger(__name__)

OPENSEARCH_RETRY_ATTEMPTS = 20
OPENSEARCH_RETRY_DELAY_SECS = 3


def _build_app(
    ol_session: aiohttp.ClientSession,
    os_session: aiohttp.ClientSession,
    upstream_base: str,
    service_name: str,
) -> web.Application:
    app = web.Application()
    app["ol_session"] = ol_session
    app["os_session"] = os_session
    app["upstream_base"] = upstream_base
    app["service_name"] = service_name
    app.router.add_route("*", "/{path_info:.*}", proxy_handler)
    return app


async def _wait_for_opensearch(os_session: aiohttp.ClientSession) -> None:
    for attempt in range(1, OPENSEARCH_RETRY_ATTEMPTS + 1):
        try:
            await ensure_index(os_session)
            return
        except Exception as exc:
            log.warning(
                "OpenSearch not ready (attempt %d/%d): %s",
                attempt, OPENSEARCH_RETRY_ATTEMPTS, exc
            )
            await asyncio.sleep(OPENSEARCH_RETRY_DELAY_SECS)
    log.error("OpenSearch never became ready after %d attempts", OPENSEARCH_RETRY_ATTEMPTS)


async def run() -> None:
    ol_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=10)
    )
    os_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10)
    )

    # Start background log workers
    for _ in range(2):
        asyncio.create_task(log_worker(os_session))

    # Wait for OpenSearch to be ready
    await _wait_for_opensearch(os_session)

    # Start one proxy per mapping
    runners = []
    for m in MAPPINGS:
        app = _build_app(ol_session, os_session, m["upstream"], m["name"])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, PROXY_HOST, m["port"])
        await site.start()
        runners.append(runner)
        log.info(
            "Started proxy '%s' on %s:%d → %s",
            m["name"], PROXY_HOST, m["port"], m["upstream"]
        )

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        log.info("Shutting down proxies...")
        for runner in runners:
            await runner.cleanup()
        await ol_session.close()
        await os_session.close()