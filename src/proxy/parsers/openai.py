import json
from .base import BaseParser

class OpenAIParser(BaseParser):
    def __repr__(self):
        return "OpenAIParser"


    def consume(self, chunk: bytes):
        if self.is_streaming:
            # print(f"Streaming chunk: {chunk}")
            self._consume_streaming(chunk)
        else:
            # print(f"Non-streaming chunk: {chunk}")
            self._consume_non_streaming(chunk)

    def _consume_streaming(self, chunk: bytes):
        # Buffer and split by newlines (SSE)
        self._buffer.extend(chunk)
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if not line or not line.startswith(b"data:"):
                continue
            
            payload = line[5:].strip()
            if payload == b"[DONE]":
                continue
            
            data = self._parse_json_safely(payload.decode("utf-8", errors="ignore"))
            if data:
                self._extract_data(data)

    def _consume_non_streaming(self, chunk: bytes):
        self._buffer.extend(chunk)
        # For non-streaming, we expect a single JSON object at the end
        # But we can try to parse it if it looks complete
        data = self._parse_json_safely(self._buffer.decode("utf-8", errors="ignore"))
        if data:
            self._extract_data(data)

    def _extract_data(self, data: dict):
        # 1. Standard OpenAI (Chat Completions)
        for choice in data.get("choices", []):
            msg = choice.get("delta") or choice.get("message") or {}
            if msg.get("content"):
                self.content_parts.append(msg["content"])
        
        # 2. OpenAI /v1/responses - Streaming Deltas
        if data.get("type") == "response.output_text.delta" and data.get("delta"):
            self.content_parts.append(data["delta"])

        # 3. OpenAI /v1/responses - Non-streaming or Final Summary
        if not self.content_parts and "output" in data:
            outputs = data["output"] if isinstance(data["output"], list) else []
            for out in outputs:
                # Some versions might have content directly, others in output items
                for item in out.get("content", []):
                    if item.get("type") == "output_text" and item.get("text"):
                        self.content_parts.append(item["text"])
        
        # 4. Token Usage Normalization
        usage = data.get("usage")
        # /v1/responses puts usage inside the response object in the 'response.completed' event
        if not usage and data.get("type") == "response.completed":
            usage = data.get("response", {}).get("usage")

        if usage:
            # Map /v1/responses keys to standard names for OpenSearch consistency
            if "input_tokens" in usage:
                self.token_info["prompt_tokens"] = usage["input_tokens"]
            if "output_tokens" in usage:
                self.token_info["completion_tokens"] = usage["output_tokens"]
            
            # Preserve original keys too
            self.token_info.update(usage)
            
            # Ensure total_tokens is calculated
            if "total_tokens" not in self.token_info:
                self.token_info["total_tokens"] = (
                    self.token_info.get("prompt_tokens", 0) + 
                    self.token_info.get("completion_tokens", 0)
                )


