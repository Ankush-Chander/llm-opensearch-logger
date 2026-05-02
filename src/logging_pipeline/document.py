import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.config import LOG_BODY_MAX_BYTES


def _truncate(raw: bytes) -> tuple[bytes, bool]:
    if len(raw) > LOG_BODY_MAX_BYTES:
        return raw[:LOG_BODY_MAX_BYTES], True
    return raw, False


def _try_json(raw: bytes) -> Optional[dict]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _extract_user_input(body: Optional[dict]) -> str:
    """Extract the text of the last user message."""
    if not body:
        return ""
    items = body.get("messages") or body.get("input") or []
    for msg in reversed(items):
        if msg.get("role") == "user":
            content = msg.get("content") or ""
            if isinstance(content, list):
                return " ".join(
                    p.get("text") or p.get("input_text") or "" 
                    for p in content if isinstance(p, dict)
                )
            return str(content)
    return ""


def _extract_conversation_id(body: Optional[dict]) -> tuple[str, int]:
    """
    Extract conversation_id and turn_number from the request body.
    Supports 'messages' and 'input' fields.
    """
    if not body:
        return str(uuid.uuid4()), 1

    messages: list = body.get("messages") or body.get("input") or []
    model: str = body.get("model", "")

    system_prompt = ""
    first_user_msg = ""
    user_turn_count = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                str(p.get("text") or p.get("input_text") or "") 
                for p in content if isinstance(p, dict)
            )
        if role == "system" and not system_prompt:
            system_prompt = content
        elif role == "user":
            user_turn_count += 1
            if not first_user_msg:
                first_user_msg = content

    if not first_user_msg:
        return str(uuid.uuid4()), 1

    # Normalize to ensure stability across turns even if client adds whitespace/newlines
    key = f"{system_prompt.strip()}|{first_user_msg.strip()}|{model}"
    conversation_id = hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()
    return conversation_id, max(user_turn_count, 1)


def build_doc(
    request_id: str,
    service_name: str,
    method: str,
    path: str,
    req_body: Optional[dict],
) -> dict:
    """
    Build a document for OpenSearch.
    """
    conversation_id, turn_number = _extract_conversation_id(req_body)
    return {
        "request_id":      request_id,
        "service":         service_name,
        "conversation_id": conversation_id,
        "turn_number":     turn_number,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "method":          method,
        "path":            path,
        "user_input":      _extract_user_input(req_body),
        "model":           (req_body or {}).get("model"),
        "request_body":    req_body,
    }