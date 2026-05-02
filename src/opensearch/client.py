import logging

import aiohttp

from src.config import OPENSEARCH_URL, OPENSEARCH_INDEX
from src.opensearch.schema import INDEX_MAPPING

log = logging.getLogger(__name__)


async def ensure_index(session: aiohttp.ClientSession) -> None:
    url = f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}"
    async with session.put(url, json=INDEX_MAPPING, headers={"Content-Type": "application/json"}) as resp:
        body = await resp.json()
        if resp.status in (200, 400):
            log.info("Index ready: %s (status=%d)", OPENSEARCH_INDEX, resp.status)
        else:
            log.warning("Unexpected index creation response: %d %s", resp.status, body)


async def index_document(session: aiohttp.ClientSession, doc: dict) -> None:
    doc_id = doc["request_id"]
    url = f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_doc/{doc_id}"
    try:
        async with session.put(url, json=doc, headers={"Content-Type": "application/json"}) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                log.warning("OpenSearch indexing failed (%d): %s", resp.status, body)
    except Exception as exc:
        log.warning("Failed to index document: %s", exc)
