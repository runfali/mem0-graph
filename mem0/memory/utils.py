import hashlib
import logging
import re
import time
from typing import Any, Dict, List

from mem0.configs.prompts import (
    AGENT_MEMORY_EXTRACTION_PROMPT,
    FACT_RETRIEVAL_PROMPT,
    USER_MEMORY_EXTRACTION_PROMPT,
)

logger = logging.getLogger(__name__)


def get_fact_retrieval_messages(message, is_agent_memory=False):
    """Get fact retrieval messages based on the memory type.
    
    Args:
        message: The message content to extract facts from
        is_agent_memory: If True, use agent memory extraction prompt, else use user memory extraction prompt
        
    Returns:
        tuple: (system_prompt, user_prompt)
    """
    if is_agent_memory:
        return AGENT_MEMORY_EXTRACTION_PROMPT, f"Input:\n{message}"
    else:
        return USER_MEMORY_EXTRACTION_PROMPT, f"Input:\n{message}"


def get_fact_retrieval_messages_legacy(message):
    """Legacy function for backward compatibility."""
    return FACT_RETRIEVAL_PROMPT, f"Input:\n{message}"


def ensure_json_instruction(system_prompt, user_prompt):
    """Ensure the word 'json' appears in the prompts when using json_object response format.

    OpenAI's API requires the word 'json' to appear in the messages when
    response_format is set to {"type": "json_object"}. When users provide a
    custom_instructions that doesn't include 'json', this causes a
    400 error. This function appends a JSON format instruction to the system
    prompt if 'json' is not already present in either prompt.

    Args:
        system_prompt: The system prompt string
        user_prompt: The user prompt string

    Returns:
        tuple: (system_prompt, user_prompt) with JSON instruction added if needed
    """
    combined = (system_prompt + user_prompt).lower()
    if "json" not in combined:
        system_prompt += (
            "\n\nYou must return your response in valid JSON format "
            "with a 'facts' key containing an array of strings."
        )
    return system_prompt, user_prompt


def parse_messages(messages):
    response = ""
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        # Skip messages without textual content (e.g. assistant tool-call
        # messages that carry `tool_calls` but no `content` key).
        if content is None:
            continue
        if role == "system":
            response += f"system: {content}\n"
        elif role == "user":
            response += f"user: {content}\n"
        elif role == "assistant":
            response += f"assistant: {content}\n"
    return response


def format_entities(entities):
    if not entities:
        return ""

    formatted_lines = []
    for entity in entities:
        simplified = f"{entity['source']} -- {entity['relationship']} -- {entity['destination']}"
        formatted_lines.append(simplified)

    return "\n".join(formatted_lines)

def normalize_facts(raw_facts):
    """Normalize LLM-extracted facts to a list of strings.

    Smaller LLMs (e.g. llama3.1:8b) sometimes return facts as objects
    like {"fact": "..."} or {"text": "..."} instead of plain strings.
    This mirrors the TypeScript FactRetrievalSchema validation.
    """
    if not raw_facts:
        return []
    normalized = []
    for item in raw_facts:
        if isinstance(item, str):
            fact = item
        elif isinstance(item, dict):
            fact = item.get("fact") or item.get("text")
            if fact is None:
                logger.warning("Unexpected fact shape from LLM, skipping: %s", item)
                continue
        else:
            fact = str(item)
        if fact:
            normalized.append(fact)
    return normalized


_THINK_TAG_PAIR_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# An unterminated <think> (no closing tag): reasoning ran into the payload,
# so everything from the opening tag onward is noise.
_THINK_TAG_UNCLOSED_RE = re.compile(r"<think>(?!.*?</think>).*$", re.DOTALL)


def _strip_think_tags(text: str) -> str:
    """Remove DeepSeek-style <think>...</think> reasoning spans (and an
    unterminated leading <think>) from a non-JSON region of an LLM reply."""
    text = _THINK_TAG_PAIR_RE.sub("", text)
    return _THINK_TAG_UNCLOSED_RE.sub("", text)


def remove_code_blocks(content: str) -> str:
    """
    Removes enclosing code block markers ```[language] and ``` from a given string.
    Also strips DeepSeek-style thinking blocks (<think>...</think> tags or
    plain "thinking ... response" markers) that appear before the JSON content.

    Remarks:
    - The function uses a regex pattern to match code blocks that may start with ``` followed by an optional language tag (letters or numbers) and end with ```.
    - If a code block is detected, it returns only the inner content, stripping out the markers.
    - If no code block markers are found, the original content is returned as-is.
    - Thinking blocks are only stripped from the prefix before the first '{' to avoid corrupting JSON content that legitimately contains "thinking"/"response"/"<think>" words.
    """
    pattern = r"^```[a-zA-Z0-9]*\n([\s\S]*?)\n```$"
    match = re.match(pattern, content.strip())
    match_res = match.group(1).strip() if match else content.strip()

    # Find where the JSON starts (first '{')
    first_brace = match_res.find("{")
    if first_brace > 0:
        # There's a prefix before the JSON — strip thinking block only from prefix
        prefix = match_res[:first_brace]
        json_part = match_res[first_brace:]
        prefix = re.sub(
            r"^\s*thinking.*?\s*response\s*",
            "", prefix, flags=re.DOTALL,
        )
        prefix = _strip_think_tags(prefix)
        return (prefix + json_part).strip()
    elif first_brace == -1:
        # No JSON found — strip thinking block from entire content (backward compat)
        match_res = re.sub(
            r"^\s*thinking.*?\s*response\s*",
            "", match_res, flags=re.DOTALL,
        )
        return _strip_think_tags(match_res).strip()

    # JSON starts at position 0 — no prefix to strip
    return match_res.strip()



def extract_json(text):
    """
    Extracts JSON content from a string, removing enclosing triple backticks and optional 'json' tag if present.
    If no code block is found, attempts to locate JSON by finding the first '{' and last '}'.
    If that also fails, returns the text as-is.
    """
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        start_idx = text.find("{")
        end_idx = text.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = text[start_idx : end_idx + 1]
        else:
            json_str = text
    return json_str


def get_image_description(image_obj, llm, vision_details):
    """
    Get the description of the image
    """

    if isinstance(image_obj, str):
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "A user is providing an image. Provide a high level description of the image and do not include any additional text.",
                    },
                    {"type": "image_url", "image_url": {"url": image_obj, "detail": vision_details}},
                ],
            },
        ]
    else:
        messages = [image_obj]

    response = llm.generate_response(messages=messages)
    return response


def parse_vision_messages(messages, llm=None, vision_details="auto"):
    """
    Parse the vision messages from the messages
    """
    returned_messages = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            returned_messages.append(msg)
            continue

        # Skip messages without content (e.g. assistant tool-call messages
        # that carry `tool_calls` but no `content` key).
        if content is None:
            continue

        # Handle message content
        if isinstance(content, list):
            if llm is None:
                text_parts = [
                    part["text"] for part in msg["content"]
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                if not text_parts:
                    continue
                returned_messages.append({"role": role, "content": " ".join(text_parts)})
            else:
                description = get_image_description(msg, llm, vision_details)
                returned_messages.append({"role": role, "content": description})
        elif isinstance(content, dict) and content.get("type") == "image_url":
            if llm is None:
                continue
            image_url_obj = content.get("image_url")
            image_url = image_url_obj.get("url") if isinstance(image_url_obj, dict) else None
            if not image_url:
                raise ValueError("image_url content part is missing image_url.url")
            try:
                description = get_image_description(image_url, llm, vision_details)
                returned_messages.append({"role": role, "content": description})
            except Exception:
                raise Exception(f"Error while downloading {image_url}.")
        else:
            # Regular text content
            returned_messages.append(msg)

    return returned_messages


def process_telemetry_filters(filters):
    """
    Process the telemetry filters
    """
    if filters is None:
        return [], {}

    encoded_ids = {}
    if "user_id" in filters:
        encoded_ids["user_id"] = hashlib.md5(filters["user_id"].encode()).hexdigest()
    if "agent_id" in filters:
        encoded_ids["agent_id"] = hashlib.md5(filters["agent_id"].encode()).hexdigest()
    if "run_id" in filters:
        encoded_ids["run_id"] = hashlib.md5(filters["run_id"].encode()).hexdigest()

    return list(filters.keys()), encoded_ids


def sanitize_relationship_for_cypher(relationship) -> str:
    """Sanitize relationship text for Cypher queries.

    Uses backtick-quoting for Unicode (Chinese) relationship type names.
    FalkorDB v42001+ supports backtick-quoted CJK identifiers natively.
    ASCII identifiers pass through when safe.
    """
    if len(relationship) >= 2 and relationship.startswith("`") and relationship.endswith("`"):
        inner = relationship[1:-1]
        if inner and "`" not in inner:
            return relationship

    _UNSAFE_RE = re.compile(r'[`{}\[\]<>()|&=+*^%$#@!?;:"\'\\\\/\s]')
    sanitized = _UNSAFE_RE.sub("_", relationship)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")

    if not sanitized:
        return "related_to"

    if re.match(r'^[a-zA-Z0-9_]+$', sanitized):
        return sanitized

    return f"`{sanitized}`"


def sanitize_label_for_cypher(label) -> str:
    """Sanitize a node label for Cypher.

    Node labels come from the extract_entities tool schema whitelist
    (person/organization/location/tool/concept/event/metric/product/user/other)
    or the ``__User__`` fallback — all ASCII identifiers. Unlike relationship
    types, labels must keep leading/trailing underscores intact (e.g.
    ``__User__``), so no underscore stripping is applied here.

    Non-ASCII labels (defensive; not expected from the whitelist) are
    backtick-quoted so they remain valid Cypher identifiers.
    """
    if not label:
        return "__User__"
    if re.match(r'^[a-zA-Z0-9_]+$', label):
        return label
    return f"`{label}`"


def remove_spaces_from_entities(
    entity_list: List[Any],
    *,
    sanitize_relationship: bool = True,
) -> List[Dict[str, Any]]:
    """
    Normalize entity relation dicts from LLM/tool output: lowercase, spaces to underscores.

    Skips entries that are not non-empty dicts or that lack any of
    ``source``, ``relationship``, or ``destination`` (avoids KeyError on ``[{}]``
    or partial dicts).
    """
    required = ("source", "relationship", "destination")
    cleaned: List[Dict[str, Any]] = []
    for item in entity_list:
        if not isinstance(item, dict) or not item:
            continue
        if not all(key in item for key in required):
            continue
        item["source"] = item["source"].lower().replace(" ", "_")
        rel = item["relationship"].lower().replace(" ", "_")
        item["relationship"] = sanitize_relationship_for_cypher(rel) if sanitize_relationship else rel
        item["destination"] = item["destination"].lower().replace(" ", "_")
        cleaned.append(item)
    return cleaned


MAX_ESTIMATE_CACHE_SIZE = 128
_token_cache: Dict[str, tuple[float, int]] = {}
_token_cache_hits = 0
_token_cache_misses = 0


def _get_tiktoken_encoder():
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _estimate_tokens(text: str) -> int:
    global _token_cache_hits, _token_cache_misses

    now = time.time()
    entry = _token_cache.get(text)
    if entry is not None:
        cached_time, cached_tokens = entry
        if now - cached_time < 60:
            _token_cache_hits += 1
            return cached_tokens

    _token_cache_misses += 1
    encoder = _get_tiktoken_encoder()
    if encoder is not None:
        tokens = len(encoder.encode(text))
    else:
        tokens = len(text) // 4

    if len(_token_cache) >= MAX_ESTIMATE_CACHE_SIZE:
        oldest_key = min(_token_cache, key=lambda k: _token_cache[k][0])
        del _token_cache[oldest_key]
    _token_cache[text] = (now, tokens)

    return tokens

