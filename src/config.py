import os

PROXY_HOST = os.getenv("PROXY_HOST", "0.0.0.0")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200").rstrip("/")
OPENSEARCH_INDEX = os.getenv("OPENSEARCH_INDEX", "ollama-traffic")
LOG_BODY_MAX_BYTES = int(os.getenv("LOG_BODY_MAX_BYTES", str(10 * 1024 * 1024)))
PROXY_MAPPINGS = os.getenv("PROXY_MAPPINGS", "")


def _parse_mappings() -> list[dict]:
    if not PROXY_MAPPINGS:
        host = os.getenv("OLLAMA_HOST", "127.0.0.1")
        port = int(os.getenv("OLLAMA_PORT", "11435"))
        listen = int(os.getenv("PROXY_PORT", "11434"))
        return [{"port": listen, "upstream": f"http://{host}:{port}", "name": "ollama"}]

    mappings = []
    for item in PROXY_MAPPINGS.split(","):
        parts = item.strip().strip("'\"").split(":")
        if len(parts) == 2:
            mappings.append({"port": int(parts[0]), "upstream": f"http://127.0.0.1:{parts[1]}", "name": "default"})
        elif len(parts) == 3:
            if parts[1].isdigit():
                mappings.append({"port": int(parts[0]), "upstream": f"http://127.0.0.1:{parts[1]}", "name": parts[2]})
            else:
                mappings.append({"port": int(parts[0]), "upstream": f"http://{parts[1]}:{parts[2]}", "name": "default"})
        elif len(parts) == 4:
            mappings.append({"port": int(parts[0]), "upstream": f"http://{parts[1]}:{parts[2]}", "name": parts[3]})
    return mappings


MAPPINGS = _parse_mappings()