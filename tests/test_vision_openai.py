"""The OpenAI-compatible transport (D28).

One client for any vendor that speaks the chat-completions format: Gemini's
compatibility layer by default, OpenRouter or a local Ollama by URL. That makes
three things worth guarding: the request it builds must carry the same question
Anthropic gets — otherwise a verdict stops meaning the same thing depending on
who answered; its failures must produce no verdict rather than a wrong one; and
the Gemini defaults must resolve without new secrets, because that is the
zero-cost path the owner chose.

The endpoint is mocked. What is under test is what this module sends and what
it does with what comes back.
"""

import json

import httpx
import pytest

from app.core.config import Settings
from app.integrations import vision
from tests.conftest import isolated_settings as settings

pytestmark = pytest.mark.asyncio

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def openai_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "AI_REVIEW_PROVIDER": "openai",
        "OPENAI_COMPAT_BASE_URL": "https://example.test/v1",
        "OPENAI_COMPAT_API_KEY": "test-key",
        "OPENAI_COMPAT_MODEL": "vision-model",
    }
    return settings(**{**base, **overrides})


def mock_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    body: object = None,
    headers: dict[str, str] | None = None,
    captured: dict[str, object] | None = None,
    calls: list[int] | None = None,
) -> None:
    """Stand in for the endpoint, capturing the request that was built."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(1)
        if captured is not None:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["json"] = json.loads(request.content)
        return httpx.Response(status, json=body, headers=headers or {})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return original(*args, transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(vision.httpx, "AsyncClient", factory)
    monkeypatch.setattr(vision, "RETRY_DELAYS", (0.0, 0.0))


def ok_body(verdict: str = "fail", confidence: float = 0.9) -> dict[str, object]:
    return {
        "model": "vision-model",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "verdict": verdict,
                            "confidence": confidence,
                            "rationale": "Debris across the basin.",
                        }
                    )
                }
            }
        ],
    }


async def ask(**overrides: object) -> vision.ReviewResult:
    kwargs: dict[str, object] = {
        "submitted": b"\xff\xd8\xff-submitted",
        "reference": None,
        "title": "Sink clean",
        "instruction": None,
        "recorded_result": "pass",
    }
    kwargs.update(overrides)
    return await vision.review(**kwargs)  # type: ignore[arg-type]


class TestRequestShape:
    async def test_reference_goes_first_and_both_images_are_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prompt says the FIRST image is the reference. If this order ever
        flipped, every comparison would silently invert."""
        captured: dict[str, object] = {}
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(monkeypatch, body=ok_body(), captured=captured)

        await ask(reference=b"\x89PNG-reference")

        content = captured["json"]["messages"][1]["content"]  # type: ignore[index]
        assert [block["type"] for block in content] == ["image_url", "image_url", "text"]
        assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    async def test_one_image_when_the_outlet_has_no_standard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(monkeypatch, body=ok_body(), captured=captured)

        await ask(reference=None)

        content = captured["json"]["messages"][1]["content"]  # type: ignore[index]
        assert [b["type"] for b in content] == ["image_url", "text"]
        assert "no reference photo" in content[-1]["text"]

    async def test_it_sends_the_same_system_prompt_as_anthropic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(monkeypatch, body=ok_body(), captured=captured)

        await ask()

        messages = captured["json"]["messages"]  # type: ignore[index]
        assert messages[0] == {"role": "system", "content": vision.SYSTEM}

    async def test_it_constrains_the_answer_to_a_strict_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(monkeypatch, body=ok_body(), captured=captured)

        await ask()

        fmt = captured["json"]["response_format"]  # type: ignore[index]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True
        assert set(fmt["json_schema"]["schema"]["required"]) == {
            "verdict",
            "confidence",
            "rationale",
        }
        assert captured["json"]["temperature"] == 0  # type: ignore[index]

    async def test_the_key_travels_as_a_bearer_token_to_the_configured_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(monkeypatch, body=ok_body(), captured=captured)

        await ask()

        assert captured["auth"] == "Bearer test-key"
        assert captured["url"] == "https://example.test/v1/chat/completions"

    async def test_a_trailing_slash_on_the_base_url_is_harmless(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            vision,
            "get_settings",
            lambda: openai_settings(OPENAI_COMPAT_BASE_URL="https://example.test/v1/"),
        )
        mock_endpoint(monkeypatch, body=ok_body(), captured=captured)
        await ask()
        assert captured["url"] == "https://example.test/v1/chat/completions"


class TestGeminiDefaults:
    """The zero-cost path: no new secrets when the endpoint is Gemini's."""

    def test_the_default_base_url_is_gemini(self) -> None:
        assert Settings.model_fields["OPENAI_COMPAT_BASE_URL"].default == GEMINI_BASE

    async def test_blank_key_and_model_fall_back_to_the_gemini_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            vision,
            "get_settings",
            lambda: settings(
                AI_REVIEW_PROVIDER="openai",
                OPENAI_COMPAT_BASE_URL=GEMINI_BASE,
                OPENAI_COMPAT_API_KEY="",
                OPENAI_COMPAT_MODEL="",
                GEMINI_API_KEY="gem-key",
                GEMINI_MODEL="gemini-3-flash-preview",
            ),
        )
        mock_endpoint(monkeypatch, body=ok_body(), captured=captured)

        await ask()

        assert captured["auth"] == "Bearer gem-key"
        assert captured["json"]["model"] == "gemini-3-flash-preview"  # type: ignore[index]
        assert captured["url"] == GEMINI_BASE + "/chat/completions"

    async def test_no_fallback_for_a_non_gemini_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank key must not quietly send the Gemini key to OpenRouter."""
        monkeypatch.setattr(
            vision,
            "get_settings",
            lambda: settings(
                AI_REVIEW_PROVIDER="openai",
                OPENAI_COMPAT_BASE_URL="https://openrouter.ai/api/v1",
                OPENAI_COMPAT_API_KEY="",
                GEMINI_API_KEY="gem-key",
            ),
        )
        with pytest.raises(vision.VisionUnavailable, match="OPENAI_COMPAT_API_KEY"):
            await ask()


class TestResult:
    async def test_it_records_the_model_that_answered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        body = ok_body()
        body["model"] = "vision-model-0925"
        mock_endpoint(monkeypatch, body=body)

        result = await ask()

        assert result.model == "vision-model-0925"
        assert result.prompt_version == vision.PROMPT_VERSION
        assert result.verdict == "fail"
        assert result.confidence == 0.9
        assert result.rationale == "Debris across the basin."
        assert result.compared_to_reference is False

    async def test_it_reports_having_compared_against_a_standard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(monkeypatch, body=ok_body())
        assert (await ask(reference=b"\xff\xd8\xffref")).compared_to_reference is True


class TestFailures:
    async def test_a_rate_limit_is_retried_then_no_verdict_rather_than_a_wrong_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(
            monkeypatch,
            status=429,
            body={"error": {"message": "Resource has been exhausted"}},
            calls=calls,
        )
        with pytest.raises(vision.VisionUnavailable, match="rate limit"):
            await ask()
        assert len(calls) == 3, "two retries, then give up honestly"

    async def test_a_503_is_retried_and_a_later_success_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        responses = iter(
            [httpx.Response(503, json={"error": "overloaded"}), httpx.Response(200, json=ok_body())]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return next(responses)

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        monkeypatch.setattr(
            vision.httpx,
            "AsyncClient",
            lambda *a, **k: original(
                *a, transport=transport, **{k_: v for k_, v in k.items() if k_ != "transport"}
            ),
        )
        monkeypatch.setattr(vision, "RETRY_DELAYS", (0.0, 0.0))
        monkeypatch.setattr(vision, "get_settings", openai_settings)

        assert (await ask()).verdict == "fail"

    async def test_the_providers_own_message_survives_into_the_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(
            monkeypatch, status=400, body={"error": {"message": "model does not support images"}}
        )
        with pytest.raises(vision.VisionUnavailable, match="does not support images"):
            await ask()

    async def test_an_off_schema_answer_is_refused_rather_than_salvaged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(
            monkeypatch,
            body={"model": "m", "choices": [{"message": {"content": "looks fine to me"}}]},
        )
        with pytest.raises(vision.VisionUnavailable, match="nothing matching the schema"):
            await ask()

    async def test_an_empty_response_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(monkeypatch, body={"model": "m", "choices": []})
        with pytest.raises(vision.VisionUnavailable):
            await ask()

    async def test_a_confidence_outside_the_range_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "get_settings", openai_settings)
        mock_endpoint(
            monkeypatch,
            body={
                "model": "m",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"verdict": "fail", "confidence": 8.5, "rationale": "x"}
                            )
                        }
                    }
                ],
            },
        )
        with pytest.raises(vision.VisionUnavailable):
            await ask()

    async def test_no_key_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            vision, "get_settings", lambda: openai_settings(OPENAI_COMPAT_API_KEY="")
        )
        with pytest.raises(vision.VisionUnavailable, match="no key is configured"):
            await ask()


class TestProviderDispatch:
    def test_anthropic_is_the_declared_default(self) -> None:
        """A missing AI_REVIEW_PROVIDER must never route production traffic at
        a free tier by accident."""
        assert Settings.model_fields["AI_REVIEW_PROVIDER"].default == "anthropic"

    def test_groq_is_no_longer_a_provider(self) -> None:
        """Its key leaked through a chat transcript and its free tier could not
        fit two photos in a request. The OpenAI-compatible path replaced it;
        a config naming it must fail loudly, not fall through to Anthropic."""
        with pytest.raises(Exception, match="groq"):
            settings(AI_REVIEW_PROVIDER="groq")

    async def test_the_default_takes_the_anthropic_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            vision,
            "get_settings",
            lambda: settings(ANTHROPIC_API_KEY="", OPENAI_COMPAT_API_KEY="present"),
        )
        with pytest.raises(vision.VisionUnavailable, match="ANTHROPIC_API_KEY"):
            await ask()
