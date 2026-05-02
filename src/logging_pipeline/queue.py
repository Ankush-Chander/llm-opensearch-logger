import asyncio
import logging

import aiohttp

from src.opensearch.client import index_document

log = logging.getLogger(__name__)

LOG_QUEUE: asyncio.Queue = asyncio.Queue(maxsize=1000)


def enqueue_log(doc: dict) -> None:
    """
    Enqueue a document for logging.
    """
    try:
        LOG_QUEUE.put_nowait(doc)
    except asyncio.QueueFull:
        log.warning("Log queue full, dropping document for request_id=%s", doc.get("request_id"))


async def log_worker(os_session: aiohttp.ClientSession) -> None:
    """
    Worker that consumes documents from the log queue and indexes them into OpenSearch.
    """
    while True:
        doc = await LOG_QUEUE.get()
        try:
            await index_document(os_session, doc)
        except Exception as exc:
            log.error("log_worker failed to index document: %s", exc)
        finally:
            LOG_QUEUE.task_done()