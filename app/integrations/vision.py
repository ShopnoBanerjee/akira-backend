"""The vision call behind the advisory photo review (D6).

One rule governs this whole module, and it is the same rule that will govern
the Stage 2 requisition parser: **the LLM parses and explains; deterministic
code decides.** What comes back here is a verdict, a confidence and a sentence
of reasoning. Nothing downstream lets it block a submission or approve a run —
a manager still decides, which is what keeps the separation-of-duties
constraint meaningful.

Kept separate from `ai_review` so the model call can be swapped, stubbed in a
test, or re-run against a newer model without touching the orchestration around
it. `run_item_ai_reviews` records the model and prompt version behind every
verdict for exactly that reason.

Two providers, one prompt. Anthropic is the production path; Groq exists so the
pipeline can be exercised end to end on a key that is easier to come by (D13).
The system prompt and the question are byte-identical between them — only the
transport differs — so a verdict means the same thing whichever answered, and
the real model id lands in the row either way.
"""

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import anthropic
import httpx
from anthropic.types import Base64ImageSourceParam, ImageBlockParam, TextBlockParam
from pydantic import BaseModel, Field

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: Bumped whenever the prompt below changes meaning. Stored on every review, so
#: a verdict can always be read against the question that produced it — and so
#: a re-run under a new prompt does not overwrite what the old one said.
PROMPT_VERSION = "2026-08-27.1"

MediaType = Literal["image/jpeg", "image/png", "image/webp"]

MEDIA_TYPES: dict[bytes, MediaType] = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
    b"RIFF": "image/webp",
}


class VisionVerdict(BaseModel):
    """What the model is constrained to return."""

    verdict: Literal["pass", "fail", "uncertain"] = Field(
        description=(
            "pass = the photo shows the task done to standard. "
            "fail = it clearly does not. "
            "uncertain = the photo does not show enough to tell."
        )
    )
    confidence: float = Field(ge=0, le=1, description="How sure you are, 0 to 1.")
    rationale: str = Field(
        max_length=400,
        description=(
            "One or two plain sentences a restaurant manager can act on, "
            "naming what you actually saw."
        ),
    )


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    confidence: float
    rationale: str
    model: str
    prompt_version: str
    latency_ms: int
    compared_to_reference: bool


class VisionUnavailable(RuntimeError):
    """No API key configured, or the provider could not be reached.

    Distinct from a verdict of `uncertain`: the model said nothing at all, so
    nothing should be written as if it had.
    """


SYSTEM = """You review photographs submitted as proof that a restaurant \
cleaning or preparation task was completed. You work for AKIRA, a Japanese \
ramen group in Kolkata.

You are advisory. A human manager makes the decision; your job is to give them \
a starting point and a reason, not a ruling.

How to judge:
- Judge only what is visible. Do not infer effort, intent, or what happened \
off-camera.
- When a reference photo is supplied, it is that outlet's own standard for \
this task. Compare against it. Differences in angle, lighting, time of day, \
or which utensils happen to be present are NOT failures — only the state of \
the thing being checked matters.
- Say `uncertain` freely. A photo too blurred, too tight, too dark, or aimed \
at the wrong thing is uncertain, not a failure. Forcing a binary produces \
confident nonsense, which is worse for a manager than admitting doubt.
- `fail` means you can see the task was not done: visible grease, debris, \
standing water, an unstocked station, a surface plainly untouched.
- Never mention people. If a person appears in the frame, ignore them.

Your rationale is read by a busy manager on a phone. One or two sentences, \
concrete, naming what you saw."""


def _media_type(image_bytes: bytes) -> MediaType:
    for signature, media_type in MEDIA_TYPES.items():
        if image_bytes.startswith(signature):
            return media_type
    # Storage only accepts these three types, so anything else means the object
    # is not what its row claims. Default to JPEG and let the API reject it,
    # rather than guessing something the provider will silently mis-decode.
    return "image/jpeg"


def _image_block(image_bytes: bytes) -> ImageBlockParam:
    return ImageBlockParam(
        type="image",
        source=Base64ImageSourceParam(
            type="base64",
            media_type=_media_type(image_bytes),
            data=base64.standard_b64encode(image_bytes).decode("ascii"),
        ),
    )


def build_prompt(
    *,
    title: str,
    instruction: str | None,
    has_reference: bool,
    recorded_result: str,
) -> str:
    """The question, as plain text. Separated out so a test can read it."""
    lines = [f"The checklist item is: {title}"]
    if instruction:
        lines.append(f"The instruction given to staff is: {instruction}")
    if has_reference:
        lines.append(
            "The first image is this outlet's reference standard for this item. "
            "The second image is what staff submitted tonight."
        )
    else:
        lines.append(
            "There is no reference photo for this item at this outlet yet, so "
            "judge the submitted image on the instruction alone and lean "
            "towards `uncertain` where a standard would have settled it."
        )
    lines.append(f"Staff recorded this item as: {recorded_result}.")
    lines.append(
        "Does the submitted photo show this task done? Answer with your verdict, "
        "your confidence, and a short rationale."
    )
    return "\n\n".join(lines)


async def review(
    *,
    submitted: bytes,
    reference: bytes | None,
    title: str,
    instruction: str | None,
    recorded_result: str,
) -> ReviewResult:
    """Ask the configured provider. Raises VisionUnavailable rather than
    inventing a verdict — silence is not the same as `uncertain`."""
    settings = get_settings()
    ask = {
        "groq": _review_groq,
        "gemini": _review_gemini,
    }.get(settings.AI_REVIEW_PROVIDER, _review_anthropic)
    return await ask(
        submitted=submitted,
        reference=reference,
        title=title,
        instruction=instruction,
        recorded_result=recorded_result,
    )


async def _review_anthropic(
    *,
    submitted: bytes,
    reference: bytes | None,
    title: str,
    instruction: str | None,
    recorded_result: str,
) -> ReviewResult:
    settings = get_settings()
    if not settings.ANTHROPIC_API_KEY:
        raise VisionUnavailable(
            "ANTHROPIC_API_KEY is not configured, so AI photo review has "
            "nothing to call. Turn ai_review.enabled off, or set the key."
        )

    # Reference first, submitted second — the prompt names them in that order,
    # and swapping them silently inverts every comparison.
    content: list[ImageBlockParam | TextBlockParam] = []
    if reference is not None:
        content.append(_image_block(reference))
    content.append(_image_block(submitted))
    content.append(
        TextBlockParam(
            type="text",
            text=build_prompt(
                title=title,
                instruction=instruction,
                has_reference=reference is not None,
                recorded_result=recorded_result,
            ),
        )
    )

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=90.0)
    started = time.monotonic()
    try:
        response = await client.messages.parse(
            model=settings.AI_REVIEW_MODEL,
            max_tokens=2000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            # Medium, not low. A verdict a manager cannot trust is worse than
            # no verdict — the same failure mode as a duplicate detector with
            # false positives, which gets switched off and then checks nothing.
            output_config={"effort": "medium"},
            output_format=VisionVerdict,
        )
    except anthropic.APIError as exc:
        raise VisionUnavailable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        await client.close()

    parsed = response.parsed_output
    if parsed is None:
        raise VisionUnavailable("The model returned nothing that matched the schema.")

    return ReviewResult(
        verdict=parsed.verdict,
        confidence=parsed.confidence,
        rationale=parsed.rationale.strip(),
        model=response.model,
        prompt_version=PROMPT_VERSION,
        latency_ms=int((time.monotonic() - started) * 1000),
        compared_to_reference=reference is not None,
    )


# ---------------------------------------------------------------------------
# Groq — the testing path (D13)
# ---------------------------------------------------------------------------

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"


async def _review_gemini(
    *,
    submitted: bytes,
    reference: bytes | None,
    title: str,
    instruction: str | None,
    recorded_result: str,
) -> ReviewResult:
    """Same system prompt, same question, Gemini transport. The free tier's
    daily quota covers an outlet's photo volume with room to spare, which is
    what pushed review off the squeezed Groq tier."""
    import asyncio as _asyncio

    settings = get_settings()
    if not settings.GEMINI_API_KEY:
        raise VisionUnavailable("GEMINI_API_KEY is not configured.")

    def _part(image: bytes) -> dict[str, Any]:
        return {
            "inline_data": {
                "mime_type": _media_type(image),
                "data": base64.b64encode(image).decode(),
            }
        }

    # Reference first, submitted second — the prompt names them in that order,
    # and swapping them silently inverts every comparison.
    parts: list[dict[str, Any]] = []
    if reference is not None:
        parts.append(_part(reference))
    parts.append(_part(submitted))
    parts.append(
        {
            "text": SYSTEM
            + "\n\n"
            + build_prompt(
                title=title,
                instruction=instruction,
                recorded_result=recorded_result,
                has_reference=reference is not None,
            )
        }
    )
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": VisionVerdict.model_json_schema(),
            "temperature": 0,
        },
    }
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(3):
            response = await client.post(
                f"{GEMINI_URL}/{settings.GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": settings.GEMINI_API_KEY},
                json=body,
            )
            if response.status_code not in (429, 503):
                break
            await _asyncio.sleep(15 * (attempt + 1))
    if response.status_code != 200:
        raise VisionUnavailable(
            f"Gemini refused the review: {response.status_code} {response.text[:200]}"
        )
    latency = int((time.monotonic() - started) * 1000)
    payload = response.json()
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise VisionUnavailable(f"Gemini returned no verdict: {exc}") from exc
    verdict = VisionVerdict.model_validate_json(text)
    return ReviewResult(
        verdict=verdict.verdict,
        confidence=verdict.confidence,
        rationale=verdict.rationale,
        model=settings.GEMINI_MODEL,
        prompt_version=PROMPT_VERSION,
        latency_ms=latency,
        compared_to_reference=reference is not None,
    )


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

#: The same three fields VisionVerdict declares, as raw JSON Schema. Groq's
#: OpenAI-compatible endpoint wants the schema inline rather than derived from
#: a model class, and `strict` makes it a constraint rather than a suggestion.
_GROQ_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
    "required": ["verdict", "confidence", "rationale"],
    "additionalProperties": False,
}


def _groq_image(image_bytes: bytes) -> dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{_media_type(image_bytes)};base64,"
            + base64.standard_b64encode(image_bytes).decode("ascii")
        },
    }


async def _review_groq(
    *,
    submitted: bytes,
    reference: bytes | None,
    title: str,
    instruction: str | None,
    recorded_result: str,
) -> ReviewResult:
    """Groq's OpenAI-compatible endpoint, over plain httpx.

    No SDK: this is one POST, httpx is already a dependency, and pulling in a
    second vendor client for a path that exists to prove the pipeline would
    cost more than it saves.
    """
    settings = get_settings()
    if not settings.GROQ_API_KEY:
        raise VisionUnavailable("AI_REVIEW_PROVIDER is groq but GROQ_API_KEY is not configured.")

    content: list[dict[str, Any]] = []
    if reference is not None:
        content.append(_groq_image(reference))
    content.append(_groq_image(submitted))
    content.append(
        {
            "type": "text",
            "text": build_prompt(
                title=title,
                instruction=instruction,
                has_reference=reference is not None,
                recorded_result=recorded_result,
            ),
        }
    )

    started = time.monotonic()
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
            json={
                "model": settings.GROQ_VISION_MODEL,
                "max_completion_tokens": 1200,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "photo_verdict",
                        "schema": _GROQ_SCHEMA,
                        "strict": True,
                    },
                },
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": content},
                ],
            },
        )

    if response.status_code == 429:
        # The free tier is 8000 tokens per minute and two photos is most of a
        # request. A background job that hit the ceiling has not failed — it
        # simply has no verdict yet, and saying so beats inventing one.
        raise VisionUnavailable(
            f"Groq rate limit reached; no verdict this time. {_groq_error(response)}"
        )
    if response.status_code >= 400:
        raise VisionUnavailable(f"Groq returned {response.status_code}: {_groq_error(response)}")

    payload = response.json()
    try:
        text_out = payload["choices"][0]["message"]["content"]
        parsed = VisionVerdict.model_validate_json(text_out)
    except (KeyError, IndexError, ValueError) as exc:
        # strict json_schema should make this impossible. If it happens the
        # honest answer is no verdict, not a guess at what was meant.
        raise VisionUnavailable(f"Groq returned nothing matching the schema: {exc}") from exc

    return ReviewResult(
        verdict=parsed.verdict,
        confidence=parsed.confidence,
        rationale=parsed.rationale.strip(),
        # The model that actually answered, not the one that was asked for.
        model=str(payload.get("model") or settings.GROQ_VISION_MODEL),
        prompt_version=PROMPT_VERSION,
        latency_ms=int((time.monotonic() - started) * 1000),
        compared_to_reference=reference is not None,
    )


def _groq_error(response: "httpx.Response") -> str:
    try:
        return str(response.json().get("error", {}).get("message", ""))[:300]
    except ValueError:
        return response.text[:300]
