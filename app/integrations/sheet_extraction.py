"""Reading a photographed stock count sheet into structured rows.

Same governing rule as vision.py, same split of responsibilities: **the model
transcribes, deterministic code decides.** What comes back here is raw text
per cell with a confidence — never a mapped item, never a normalised number.
`normalize.py` and `mapping.py` own those, so a transcription error surfaces
as an unmatched or refused row a human reviews, not a wrong number in a
requisition.

Two providers, one contract. The Anthropic path is the production one:
row-alignment on handwritten sheets is exactly the kind of visual task where
the measured difference is decisive — the Groq path (tested against the real
27 Aug sheet) shifted handwritten values onto neighbouring rows at 0.9
confidence, which is the one failure mode this pipeline cannot tolerate
quietly. It stays as the plumbing-test fallback, and everything it produces
is forced into review (see counts_service).

The sheets are printed FROM the catalogue, so the prompt carries the
catalogue's item vocabulary. That anchors printed-name transcription (the
Groq experiments hallucinated "Broccoli" for Bokchoy without it) while the
handwriting is still transcribed verbatim.
"""

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx
from anthropic.types import Base64ImageSourceParam, ImageBlockParam, TextBlockParam
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.integrations.vision import GROQ_URL, VisionUnavailable, _media_type

logger = logging.getLogger(__name__)

#: Stored on every count (stock_counts.extractor, with the model id) so a
#: re-extraction under a newer prompt is distinguishable from the original.
EXTRACTOR_VERSION = "sheet.v1.2026-08-27"


class ExtractedRow(BaseModel):
    """One printed row of the sheet, transcribed — never interpreted."""

    sl_no: int | None = Field(default=None, description="The printed Sl No., if legible.")
    item_name: str = Field(description="The printed item name, transcribed exactly.")
    closing_count_raw: str | None = Field(
        default=None,
        description=(
            "The handwritten Physical Closing Count cell EXACTLY as written "
            "('1.500', '1kg', '5pk', '0'). null when the cell is blank. "
            "Never convert units, never normalise."
        ),
    )
    requisition_raw: str | None = Field(
        default=None,
        description="The handwritten Requisition Qty cell, same rules. null when blank.",
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description=(
            "Your certainty that the HANDWRITTEN cells of this row are "
            "transcribed correctly and on the correct row. Printed-only rows "
            "with blank cells are 1.0. Use below 0.6 whenever the handwriting "
            "is genuinely hard or a value could belong to a neighbouring row."
        ),
    )


class ExtractedPage(BaseModel):
    """What the model is constrained to return for one page."""

    sheet_date: str | None = Field(
        default=None, description="The handwritten Date field as YYYY-MM-DD, or null."
    )
    counted_at_label: str | None = Field(
        default=None, description="The handwritten Time field verbatim ('3 PM'), or null."
    )
    rows: list[ExtractedRow]


@dataclass(frozen=True)
class PageResult:
    page: ExtractedPage
    model: str
    extractor_version: str
    latency_ms: int


SYSTEM = """You transcribe photographed stock count sheets for AKIRA, a \
Japanese ramen group in Kolkata. Each sheet is a printed table — Sl No., \
Category, Department, Item Name, Bengali Name, Unit — with two HANDWRITTEN \
columns at the right: Physical Closing Count, then Requisition Qty Need.

You are a transcriber, not an interpreter:
- Copy handwriting EXACTLY as written: "1.500" stays "1.500", "1kg" stays \
"1kg", "5pk" stays "5pk". Never convert units or normalise numbers.
- A blank cell is null. Never write 0 for a blank — a circled zero is a \
count, a blank is the absence of one, and the kitchen means different \
things by them.
- Row alignment is the one thing you must not get wrong. Trace each \
handwritten value to its printed row along the ruled line before writing it \
down. When a value sits between rows or could belong to either neighbour, \
put it on your best row with confidence below 0.6 — a human will look.
- Include every printed row, even when both handwritten cells are blank.
- Item names are printed and come from the fixed catalogue you are given. \
Transcribe the printed text; if it clearly matches a catalogue name, use the \
catalogue spelling. Never substitute a similar-looking word that is not on \
the sheet."""


def build_prompt(vocabulary: list[str]) -> str:
    vocab = "\n".join(f"- {name}" for name in vocabulary)
    return (
        "Transcribe this stock count sheet page completely.\n\n"
        "The printed item names on AKIRA's sheets come from this catalogue:\n"
        f"{vocab}\n\n"
        "Remember: handwriting verbatim, blanks as null, and row alignment "
        "checked against the ruled lines before you commit a value to a row."
    )


async def extract_page(
    image_bytes: bytes,
    *,
    vocabulary: list[str],
) -> PageResult:
    """One page image in, transcribed rows out. Raises VisionUnavailable when
    no provider is reachable — the job records that as a failure, it is never
    written as an empty count."""
    settings = get_settings()
    if settings.STOCK_EXTRACT_PROVIDER == "groq":
        return await _extract_groq(image_bytes, vocabulary=vocabulary)
    return await _extract_anthropic(image_bytes, vocabulary=vocabulary)


async def _extract_anthropic(image_bytes: bytes, *, vocabulary: list[str]) -> PageResult:
    settings = get_settings()
    if not settings.ANTHROPIC_API_KEY:
        raise VisionUnavailable(
            "ANTHROPIC_API_KEY is not configured and STOCK_EXTRACT_PROVIDER is "
            "anthropic. Set the key, or switch the provider."
        )
    content: list[ImageBlockParam | TextBlockParam] = [
        ImageBlockParam(
            type="image",
            source=Base64ImageSourceParam(
                type="base64",
                media_type=_media_type(image_bytes),
                data=base64.b64encode(image_bytes).decode(),
            ),
        ),
        TextBlockParam(type="text", text=build_prompt(vocabulary)),
    ]
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=180.0)
    started = time.monotonic()
    try:
        response = await client.messages.parse(
            model=settings.STOCK_EXTRACT_MODEL,
            max_tokens=8000,
            system=SYSTEM,
            # Adaptive thinking: tracing thirty handwritten values along ruled
            # lines is exactly the kind of work where letting the model think
            # buys row alignment, which is the failure mode that matters.
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": content}],
            output_format=ExtractedPage,
        )
    except anthropic.APIError as exc:
        raise VisionUnavailable(f"Anthropic could not read the sheet: {exc}") from exc
    finally:
        await client.close()
    latency = int((time.monotonic() - started) * 1000)
    parsed = response.parsed_output
    if parsed is None:
        raise VisionUnavailable("Anthropic returned no parseable transcription.")
    return PageResult(
        page=parsed,
        model=settings.STOCK_EXTRACT_MODEL,
        extractor_version=EXTRACTOR_VERSION,
        latency_ms=latency,
    )


async def _extract_groq(image_bytes: bytes, *, vocabulary: list[str]) -> PageResult:
    """The plumbing-test path. Measured on the real sheet: names anchor well,
    handwritten values land on wrong rows with high confidence — so the
    caller treats every Groq row as needing review regardless of what the
    confidence claims."""
    settings = get_settings()
    if not settings.GROQ_API_KEY:
        raise VisionUnavailable("GROQ_API_KEY is not configured.")

    schema_hint = (
        'Return ONLY JSON: {"sheet_date": "YYYY-MM-DD or null", '
        '"counted_at_label": "verbatim or null", '
        '"rows": [{"sl_no": 1, "item_name": "...", "closing_count_raw": "... or null", '
        '"requisition_raw": "... or null", "confidence": 0.0}]}'
    )
    body: dict[str, Any] = {
        "model": settings.GROQ_VISION_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt(vocabulary) + "\n\n" + schema_hint},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,"
                            + base64.b64encode(image_bytes).decode()
                        },
                    },
                ],
            },
        ],
        "temperature": 0,
        # The free tier pre-checks prompt + max_tokens against its 8k
        # tokens-per-minute ceiling and answers 413 when the SUM exceeds it —
        # a page image is ~2k prompt tokens, so the output budget has to leave
        # room. A 30-row page measured ~1.7k completion tokens; 4000 is head
        # room without tripping the gate.
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=180) as client:
        # The free tier is 8k tokens/min and one page is most of that, so a
        # multi-page sheet WILL hit 429 between pages. Waiting it out is the
        # whole retry policy; anything cleverer belongs on the paid tier.
        for attempt in range(4):
            response = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                json=body,
            )
            if response.status_code != 429:
                break
            retry_after = float(response.headers.get("retry-after", 25))
            logger.info("groq rate limited; waiting %.0fs (attempt %d)", retry_after, attempt + 1)
            await asyncio.sleep(min(retry_after + 1, 90))
    if response.status_code != 200:
        raise VisionUnavailable(
            f"Groq refused the extraction: {response.status_code} {response.text[:200]}"
        )
    latency = int((time.monotonic() - started) * 1000)
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    page = ExtractedPage.model_validate_json(content)
    return PageResult(
        page=page,
        model=str(payload.get("model") or settings.GROQ_VISION_MODEL),
        extractor_version=EXTRACTOR_VERSION,
        latency_ms=latency,
    )
