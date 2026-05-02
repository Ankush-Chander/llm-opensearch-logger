from .openai import OpenAIParser
from .ollama import OllamaParser
from .base import BaseParser

class FallbackParser(BaseParser):
    def consume(self, chunk: bytes):
        # Very basic heuristic for unknown formats
        self._buffer.extend(chunk)
        text = self._buffer.decode("utf-8", errors="ignore")
        # Just a placeholder, really should log that we are in fallback
        pass

def get_parser(service_name: str, path: str, is_streaming: bool) -> BaseParser:
    service_name = service_name.lower()
    
    if "openai" in service_name or path.startswith("/v1"):
        return OpenAIParser(is_streaming)
    
    if "ollama" in service_name or path.startswith("/api"):
        return OllamaParser(is_streaming)
    
    return FallbackParser(is_streaming)
