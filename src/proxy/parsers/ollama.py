import json
from .base import BaseParser

class OllamaParser(BaseParser):
    def consume(self, chunk: bytes):
        # Ollama uses NDJSON (one JSON object per line)
        self._buffer.extend(chunk)
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            
            data = self._parse_json_safely(line.decode("utf-8", errors="ignore"))
            if data:
                self._extract_data(data)
        
        # Also try parsing the remaining buffer (for non-streaming or last chunk)
        if self._buffer:
            data = self._parse_json_safely(self._buffer.decode("utf-8", errors="ignore"))
            if data:
                self._extract_data(data)
                self._buffer.clear()

    def _extract_data(self, data: dict):
        # /api/chat format
        message = data.get("message", {})
        if message.get("content"):
            self.content_parts.append(message["content"])

        # /api/generate format
        if data.get("response"):
            self.content_parts.append(data["response"])

        # Token counts (usually in the final chunk where done=true)
        if data.get("done"):
            if "prompt_eval_count" in data:
                self.token_info = {
                    "prompt_tokens":     data.get("prompt_eval_count"),
                    "completion_tokens": data.get("eval_count"),
                    "total_tokens":      (data.get("prompt_eval_count", 0) + 
                                         data.get("eval_count", 0)),
                }
