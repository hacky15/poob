"""Utilities for extracting text and JSON from LLM and browser responses.

Handles format differences across providers:
- Plain string responses (Groq, OpenRouter, Ollama)
- List-of-parts responses (Gemini 3+, newer Google AI models)
- Thinking model output (reasoning chains before JSON)
"""

from __future__ import annotations

import json
import re
from typing import Any


_FB_UI_NOISE_RE = re.compile(
    r"!\[.*?\]\([^)]*\)"                        # markdown images ![alt](url)
    r"|!\[.*?\]"                                 # broken image markdown
    r"|\[.*?\]\(https?://[^)]*\)"               # markdown links [text](url)
    r"|https?://\S+"                             # bare URLs (CDN image links, etc.)
    r"|##?\s*chat history is missing"
    r"|\bhas new content\b"
    r"|\bunread message:?\b"
    r"|\b\d+\s+unread chats?\b"
    r"|\b\d+\s+new messages?\b"
    r"|\benter your pin to restore chat history\.?\b"
    r"|\buse a one-time code instead\b"
    r"|\bunread message\b"
    r"|\b\d+m\b",                               # "3m" (minutes ago badge)
    re.IGNORECASE | re.DOTALL,
)

# Single-word or short phrases that are ONLY ever Facebook nav chrome
_FB_NAV_LABELS = frozenset({
    "all", "unread", "groups", "communities", "marketplace",
    "more", "chats", "# chats", "#chats", "has new content",
})


def parse_evaluate_result(raw: Any) -> Any:
    """Parse the result of browser-use's ``Page.evaluate()``.

    browser-use returns JSON-stringified values for dicts/lists and empty
    string for None.  This function converts them back to Python objects.

    Shared utility — used by ``detail_extractor.py``, ``patrol_scanner.py``,
    and any other code that calls ``page.evaluate()`` via browser-use.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def clean_fb_description(text: str | None) -> str:
    """Strip Facebook UI chrome from a scraped listing description.

    Facebook's DOM extractor sometimes captures Messenger sidebar elements
    (navigation labels, chat counts, PIN prompts) alongside the listing text.
    This function removes those artifacts so only the seller's actual words remain.
    """
    if not text:
        return ""

    # Remove known UI patterns
    text = _FB_UI_NOISE_RE.sub(" ", text)

    # Split and filter line-by-line
    clean: list[str] = []
    for line in re.split(r"[\n\r]+", text):
        stripped = line.strip()
        if not stripped:
            continue
        # Drop pure UI nav labels
        if stripped.lower() in _FB_NAV_LABELS:
            continue
        # Drop lines that are only digits (chat/message counts)
        if re.match(r"^\d+$", stripped):
            continue
        # Drop lines that are only dashes or hyphens (dividers)
        if re.match(r"^[-\u2014\u2013\s]+$", stripped):
            continue
        clean.append(stripped)

    result = " ".join(clean)
    # Strip leftover punctuation artifacts after URL removal (e.g. ": ·", "· ")
    result = re.sub(r"^[\s:·•\-–—|]+|[\s:·•\-–—|]+$", "", result)
    result = re.sub(r"\s*[·•]\s*", " ", result)   # mid-string bullets
    return re.sub(r" {2,}", " ", result).strip()


def extract_text(content: Any) -> str:
    """Extract plain text from an LLM response content field.

    Handles multiple formats:
    - Plain string: returned as-is
    - List of content parts (Gemini 3+): extracts and joins 'text' fields
    - Other: str() fallback
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                texts.append(part["text"])
            elif isinstance(part, str):
                texts.append(part)
            else:
                # Handle LangChain content block objects with .text attribute
                text_attr = getattr(part, "text", None)
                if text_attr:
                    texts.append(str(text_attr))
        if texts:
            return "\n".join(texts)
    return str(content)


# Keys that identify our VLM evaluation JSON schema.
_VLM_SCHEMA_KEYS = {"deal_quality", "item_identified", "estimated_value_mid"}


def extract_json(text: str, *, schema_keys: set[str] | None = None) -> dict | None:
    """Extract a JSON object from text that may contain thinking/reasoning.

    Handles:
    - Clean JSON strings
    - Markdown-fenced JSON (```json ... ```)
    - Thinking model output (reasoning before/after JSON)
    - Multiple JSON-like blocks (picks the one matching schema_keys)

    Args:
        text: Raw text that may contain a JSON object.
        schema_keys: Expected keys in the target JSON object. If provided,
            only returns objects containing at least one of these keys.
            Defaults to VLM evaluation schema keys.

    Returns:
        Parsed dict, or None if no valid JSON found.
    """
    if schema_keys is None:
        schema_keys = _VLM_SCHEMA_KEYS

    text = text.strip()

    # Strip markdown fences
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    # Fast path: entire text is valid JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Find all top-level JSON objects by matching braces.
    # Walk through the string tracking brace depth to find complete objects.
    candidates: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            in_string = False
            escape_next = False
            start = i
            for j in range(i, len(text)):
                ch = text[j]
                if escape_next:
                    escape_next = False
                    continue
                if ch == "\\":
                    escape_next = True
                    continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start:j + 1])
                        i = j
                        break
            else:
                # Unbalanced — skip this opening brace
                pass
        i += 1

    # Try candidates from largest to smallest (the real JSON is usually the biggest)
    candidates.sort(key=len, reverse=True)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if not isinstance(data, dict):
                continue
            # If schema_keys provided, verify at least one key matches
            if schema_keys and not schema_keys.intersection(data.keys()):
                continue
            return data
        except json.JSONDecodeError:
            continue

    return None
