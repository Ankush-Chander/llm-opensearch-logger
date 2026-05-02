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
        # Split into [listen_port, rest]
        parts = item.strip().strip("'\"").split(":", 1)
        if len(parts) != 2:
            continue

        listen_port = int(parts[0])
        rest = parts[1]

        # Check if the rest contains a name tag at the end (e.g. "https://api.openai.com:openai")
        # We split from the right, but only if the last part isn't just a port number
        sub_parts = rest.rsplit(":", 1)
        if len(sub_parts) == 2 and not sub_parts[1].isdigit():
            upstream = sub_parts[0]
            name = sub_parts[1]
        else:
            upstream = rest
            name = "default"

        # Ensure upstream has a scheme
        if not upstream.startswith(("http://", "https://")):
            if ":" in upstream:
                upstream = f"http://{upstream}"
            else:
                upstream = f"http://127.0.0.1:{upstream}"

        mappings.append({
            "port": listen_port,
            "upstream": upstream.rstrip("/"),
            "name": name
        })
    return mappings


MAPPINGS = _parse_mappings()