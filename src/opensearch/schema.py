INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "request_id":           {"type": "keyword"},
            "service":              {"type": "keyword"},
            "conversation_id":      {"type": "keyword"},
            "turn_number":          {"type": "integer"},
            "timestamp":            {"type": "date"},
            "method":               {"type": "keyword"},
            "path":                 {"type": "keyword"},
            "query_string":         {"type": "keyword"},
            "request_headers":      {"type": "object",  "enabled": False},
            "request_body":         {"type": "object",  "enabled": True},
            "response_status":      {"type": "integer"},
            "response_content":     {"type": "text"},
            "response_reasoning":   {"type": "text",    "index": False},
            "duration_ms":          {"type": "float"},
            "model":                {"type": "keyword"},
            "prompt_tokens":        {"type": "integer"},
            "completion_tokens":    {"type": "integer"},
            "total_tokens":         {"type": "integer"},
            "truncated":            {"type": "boolean"},
            "error":                {"type": "text"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0
    }
}