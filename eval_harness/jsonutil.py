import json
import re

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(raw: str) -> dict:
    """Models sometimes add stray text or markdown fences around the JSON
    despite instructions not to - grab the first {...} block rather than
    requiring the whole string to be an exact match."""
    cleaned = _FENCE_RE.sub("", raw).strip()
    match = _JSON_OBJECT_RE.search(cleaned)
    if not match:
        raise json.JSONDecodeError("no JSON object found", cleaned, 0)
    return json.loads(match.group(0))
