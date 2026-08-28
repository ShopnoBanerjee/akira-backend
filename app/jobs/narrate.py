"""One model-written paragraph at the top of the daily digest.

The spec's split, applied to prose: **code computes every number, the model
only narrates the numbers it was handed.** The facts list below is the entire
model input — if a figure is not in it, the narrative cannot contain it, and
a narrative that invents one anyway is contradicted by the table printed
directly beneath it, which is exactly where a reader's trust should sit.

Advisory to the bone: no key, a rate limit, a provider outage — the digest
sends without the paragraph and the job detail says why. A morning email must
never be hostage to a model.
"""

import asyncio
import logging
import time
from datetime import date

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"

SYSTEM = """You write the opening paragraph of a daily operations digest for \
AKIRA, a Japanese ramen group in Kolkata. The reader is the owner, on a \
phone, before the day starts.

Rules:
- Use ONLY the facts you are given. Never compute, never estimate, never \
add a number that is not in the list. The full table follows your paragraph, \
so an invented figure is instantly visible as a lie.
- Two or three sentences. Lead with what deserves attention today; if \
nothing does, say the day was clean in one sentence.
- Plain words. No exclamation marks, no cheerleading, no "great job". A \
missed checklist is "missed", not "an opportunity".
- Amounts are already formatted (₹ with Indian grouping). Use them as given."""


def build_facts(
    *,
    outlet_code: str,
    business_date: date,
    headline: str,
    completion_rate: float | None,
    mean_score: float | None,
    missed: int,
    critical_fails: int,
    open_exceptions: int,
    stale_exceptions: int,
    net_display: str | None,
    net_target_display: str | None,
) -> list[str]:
    """The model's entire world, as flat statements. Pure, so a test can hold
    down exactly what the narrative is allowed to know."""
    facts = [
        f"Outlet: {outlet_code}",
        f"Trading day: {business_date}",
        f"Checklists: {headline}",
    ]
    if completion_rate is not None:
        facts.append(f"Completion rate: {completion_rate:.0f}%")
    if mean_score is not None:
        facts.append(f"Mean approved run score: {mean_score:.0f}%")
    if missed:
        facts.append(f"Missed checklists: {missed}")
    if critical_fails:
        facts.append(f"Critical failures: {critical_fails}")
    if open_exceptions:
        facts.append(f"Exceptions still open: {open_exceptions}")
    if stale_exceptions:
        facts.append(f"Critical exceptions open more than 48 hours: {stale_exceptions}")
    if net_display is not None:
        target = f" against a target of {net_target_display}" if net_target_display else ""
        facts.append(f"Net sales for the day: {net_display}{target}")
    return facts


async def narrate(facts: list[str]) -> str | None:
    """The paragraph, or None with a log line — never an exception. The
    digest's job detail records which it was."""
    settings = get_settings()
    if not settings.GEMINI_API_KEY:
        logger.info("digest narrative skipped: no GEMINI_API_KEY")
        return None

    body = {
        "contents": [
            {
                "parts": [
                    {
                        "text": SYSTEM
                        + "\n\nToday's facts:\n"
                        + "\n".join(f"- {fact}" for fact in facts)
                        + "\n\nWrite the opening paragraph."
                    }
                ]
            }
        ],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000},
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            for attempt in range(2):
                response = await client.post(
                    f"{GEMINI_URL}/{settings.GEMINI_MODEL}:generateContent",
                    headers={"x-goog-api-key": settings.GEMINI_API_KEY},
                    json=body,
                )
                if response.status_code not in (429, 503):
                    break
                await asyncio.sleep(10 * (attempt + 1))
        if response.status_code != 200:
            logger.info("digest narrative skipped: %s", response.status_code)
            return None
        text = str(response.json()["candidates"][0]["content"]["parts"][0]["text"]).strip()
    except (httpx.HTTPError, KeyError, IndexError) as exc:
        logger.info("digest narrative skipped: %s", exc)
        return None
    if not text:
        return None
    logger.info("digest narrative in %dms", int((time.monotonic() - started) * 1000))
    return text
