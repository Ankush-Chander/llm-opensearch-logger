from abc import ABC, abstractmethod
import json
import logging

log = logging.getLogger(__name__)

class BaseParser(ABC):
    def __init__(self, is_streaming: bool):
        self.is_streaming = is_streaming
        self.content_parts = []
        self.token_info = {}
        self._buffer = bytearray()

    @abstractmethod
    def consume(self, chunk: bytes):
        """Process raw bytes from the upstream response."""
        pass

    def finalize(self) -> tuple[str, dict]:
        """Return the aggregated content and token info."""
        return "".join(self.content_parts), self.token_info

    def _parse_json_safely(self, text: str) -> dict | None:
        try:
            return json.loads(text)
        except Exception:
            return None
