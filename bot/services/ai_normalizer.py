from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from bot.config import settings
from bot.services import learning, metrics
from bot.services.phone_parser import normalize_caption

logger = logging.getLogger(__name__)

AI_BASE_URL = settings.ai_base_url
AI_MODEL = settings.ai_model
AI_TOKEN = settings.ai_token


SYSTEM_PROMPT = r'''
You are a strict product-model normalizer for a Telegram phone/accessories catalog.
Your task is to canonicalize phone model names found in messy Persian/English retail posts.

Rules:
1. Output ONLY JSON: {"models":["..."]}.
2. Preserve explicit iPhone compatibility groups written with slash as ONE item, e.g. "iphone 7/8" -> "iPhone 7/8". For Samsung, Xiaomi, Redmi, and POCO catalog lists, a slash separates distinct phone models: expand EVERY token using the section/family context and retain each token's own number and suffix. Example: "NOTE11/11S/12S" MUST become three items: "Redmi Note 11", "Redmi Note 11S", "Redmi Note 12S"—never just Note 12S. Also: "A16/A26" -> "A16", "A26"; "Note9PRO/9S" -> "Redmi Note 9 Pro", "Redmi Note 9S"; "A5/C71" -> "Redmi A5", "POCO C71". Never collapse non-iPhone models into one slash-joined item.
3. Never turn an accessory list (AirPods, cases, watches, etc.) into phone models.
4. iPhone: canonical prefix is exactly "iPhone". Normalize spacing/case: 17promax -> iPhone 17 Pro Max; 14Pro -> iPhone 14 Pro; Xsmax -> iPhone XS Max.
5. Samsung: REMOVE the word "Samsung" from output. Keep model identity exactly, including the lowercase s in A21s. A21 s -> A21s, NOT A21. Keep FE, Ultra, Plus, and network suffixes such as 4G/5G when present.
6. Xiaomi: REMOVE only the generic brand word "Xiaomi" from output, but DO NOT remove "Redmi" when it is part of the product name. In a Xiaomi/Redmi Note section, canonical Note names use "Redmi Note ...". Keep the S suffix attached to its model number. Examples: Note12 4G -> Redmi Note 12 4G; Note 12S -> Redmi Note 12S; Note 13 pro plus -> Redmi Note 13 Pro Plus.
7. Do not invent a 4G/5G suffix when the source does not contain enough evidence. Prefer exact evidence over guessing.
8. Deduplicate identical canonical models.
9. Sort naturally by brand order iPhone, Samsung-family, Xiaomi-family; within each family sort by model number ascending, then variants in a sensible order.
10. Do not output brand names "Samsung" or "Xiaomi" as prefixes. "Redmi" is allowed and required for Redmi Note models.
11. Return an empty array when no phone models can be extracted.
'''


def _endpoint() -> str:
    base = (AI_BASE_URL or "").rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _extract_json(content: str) -> dict[str, Any]:
    content = content.strip()
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", content, flags=re.S)
    if not match:
        raise ValueError("AI returned non-JSON output")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("AI JSON root is not an object")
    return data


def _clean_model_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("AI response does not contain a models array")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        value = re.sub(r"\s+", " ", value.strip(" |,;\n\t"))
        if not value:
            continue
        # Hard safety rules after AI.
        value = re.sub(r"(?i)^Samsung\s+", "", value).strip()
        value = re.sub(r"(?i)^Xiaomi\s+(?=Redmi\b)", "", value).strip()
        if re.fullmatch(r"(?i)(?:case|airpods?|apple watch|watch)\b.*", value):
            continue
        # Outside iPhone compatibility labels, slash groups in this catalog are
        # shorthand for separate phone variants. Keep the deterministic parser's
        # expanded entries instead of letting an AI group hide or duplicate them.
        if "/" in value and not re.search(r"(?i)\biPhone\b", value):
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _deterministic_is_safe(candidate: str) -> bool:
    """False when the deterministic list has shapes the parser is known to get wrong."""
    if not candidate:
        return False
    if re.search(r"(?i)\bSamsung\s+", candidate):
        return False
    if re.search(r"(?i)\bXiaomi\s+(?!Redmi\b)", candidate):
        return False
    return not re.search(r"(?i)\bA\d{1,3}\s+s\b", candidate)


async def ai_normalize(
    raw_text: str, deterministic: str, job_log: Any | None = None
) -> str:
    """Canonicalize messy model lists with an OpenAI-compatible model.

    ``job_log`` is still accepted (a legacy of the OPTION bot) but the real
    logging goes through :mod:`logging`: previously a throw-away adapter swallowed
    every message, so an AI outage — a bad token, a 429, a 90-second timeout —
    was completely invisible and the bot silently ran on the deterministic
    parser alone.
    """

    def note(level: int, message: str, *args: Any) -> None:
        if job_log is not None:
            try:
                job_log.add(level, message, *args)
            except Exception:
                pass
        logger.log(level, message, *args)

    if not AI_BASE_URL or not AI_TOKEN or not AI_MODEL:
        # No AI: the deterministic parser alone. It is not "unsafe" in a way we
        # can fix here (there is no second opinion to fall back to), but the shop
        # must be able to see that this is the reason for a misread.
        if not _deterministic_is_safe(deterministic):
            note(
                logging.WARNING,
                "AI is not configured and the deterministic model list looks suspicious: %r",
                deterministic,
            )
        else:
            note(logging.INFO, "AI is not configured; using the deterministic parser only.")
        return deterministic

    learned = learning.rules_for_prompt(raw_text)
    rules_block = f"\n\nLEARNED OWNER RULES (apply exactly):\n{learned}" if learned else ""
    payload = {
        "model": AI_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + rules_block},
            {
                "role": "user",
                "content": (
                    "RAW TELEGRAM TEXT:\n"
                    + raw_text
                    + "\n\nDETERMINISTIC CANDIDATE (may be imperfect):\n"
                    + (deterministic or "<empty>")
                    + "\n\nCanonicalize and return only the JSON object."
                ),
            },
        ],
        "response_format": {"type": "json_object"},
    }
    endpoint = _endpoint()
    headers = {"Authorization": f"Bearer {AI_TOKEN}", "Content-Type": "application/json"}
    note(logging.INFO, "Calling AI model=%s endpoint=%s", AI_MODEL, endpoint)
    metrics.incr("ai_calls")
    started = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.ai_timeout_seconds, connect=15.0)) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        metrics.observe("ai_latency_ms", metrics.elapsed(started))
        content = body["choices"][0]["message"]["content"]
        data = _extract_json(content)
        models = _clean_model_list(data.get("models"))

        # The normalizer may improve spelling, but it is not allowed to silently
        # delete a deterministic candidate. That happened with long slash-heavy
        # Xiaomi/POCO lists: the AI returned a plausible, shorter subset and the
        # missing phones were never shown for review.
        canonical_candidate = normalize_caption(deterministic)
        candidate_models = _clean_model_list(canonical_candidate.split(" | ") if canonical_candidate else [])
        ai_identities: set[str] = set()
        for model in models:
            normalized = normalize_caption(model)
            ai_identities.update(
                item.casefold() for item in normalized.split(" | ") if item.strip()
            )
            if not normalized:
                ai_identities.add(model.casefold())
        missing = [
            model for model in candidate_models
            if model.casefold() not in ai_identities
        ]
        if missing:
            models.extend(missing)
            note(
                logging.WARNING,
                "AI omitted %d deterministic model(s); preserving them: %s",
                len(missing),
                " | ".join(missing[:12]),
            )
        note(logging.INFO, "AI returned %d models (%d deterministic candidates retained).", len(models), len(candidate_models))
        return " | ".join(models)
    except Exception as exc:
        metrics.incr("ai_failures")
        note(
            logging.WARNING,
            "AI normalization failed (%s: %s); falling back to the deterministic parser.",
            type(exc).__name__,
            str(exc)[:300],
        )
        return deterministic
