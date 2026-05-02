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


def _extract_conversation_id(body: Optional[dict]) -> tuple[str, int]:
    """
    Extract conversation_id and turn_number from the request body.
    """
    if not body:
        return str(uuid.uuid4()), 1

    messages: list = body.get("messages") or []
    model: str = body.get("model", "")

    system_prompt = ""
    first_user_msg = ""
    user_turn_count = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if role == "system" and not system_prompt:
            system_prompt = content
        elif role == "user":
            user_turn_count += 1
            if not first_user_msg:
                first_user_msg = content

    if not first_user_msg:
        return str(uuid.uuid4()), 1

    key = f"{system_prompt}|{first_user_msg}|{model}"
    conversation_id = hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()
    return conversation_id, max(user_turn_count, 1)


def _extract_token_counts(body: Optional[dict]) -> dict:
    """
    Extract token counts from the request body.
    """
    if not body:
        return {}
    result = {}
    if "prompt_eval_count" in body:
        result["prompt_tokens"] = body.get("prompt_eval_count")
    if "eval_count" in body:
        result["completion_tokens"] = body.get("eval_count")
    usage = body.get("usage") or {}
    if "prompt_tokens" in usage:
        result["prompt_tokens"] = usage["prompt_tokens"]
    if "completion_tokens" in usage:
        result["completion_tokens"] = usage["completion_tokens"]
    if "prompt_tokens" in result and "completion_tokens" in result:
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    return result


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
        "model":           (req_body or {}).get("model"),
        "request_body":    req_body,
    }