"""The Groq transport (D13).

Groq exists so the review pipeline can be exercised end to end on a key that is
easier to come by than an Anthropic one. That makes two things worth guarding:
the request it builds must carry the same question Anthropic gets — otherwise a
verdict stops meaning the same thing depending on who answered — and its
failures must produce no verdict rather than a wrong one.

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


def groq_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "AI_REVIEW_PROVIDER": "groq",
        "GROQ_API_KEY": "test-key",
        "GROQ_VISION_MODEL": "qwen/qwen3.8-27b",
    }
    return settings(**{**base, **overrides})


def mock_groq(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    body: object = None,
    captured: dict[str, object] | None = None,
) -> None:
    """Stand in for Groq's endpoint, capturing the request that was built."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["json"] = json.loads(request.content)
        return httpx.Response(status, json=body)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return original(*args, transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(vision.httpx, "AsyncClient", factory)


def ok_body(verdict: str = "fail", confidence: float = 0.9) -> dict[str, object]:
    return {
        "model": "qwen/qwen3.8-27b",
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
        flipped, every comparison would silently invert and the system would
        confidently report the opposite of the truth."""
        captured: dict[str, object] = {}
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(monkeypatch, body=ok_body(), captured=captured)

        await ask(reference=b"\x89PNG-reference")

        payload = captured["json"]
        assert isinstance(payload, dict)
        content = payload["messages"][1]["content"]
        assert [block["type"] for block in content] == ["image_url", "image_url", "text"]
        # The reference is a PNG and the submission a JPEG, so order is checkable.
        assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    async def test_one_image_when_the_outlet_has_no_standard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(monkeypatch, body=ok_body(), captured=captured)

        await ask(reference=None)

        content = captured["json"]["messages"][1]["content"]  # type: ignore[index]
        assert [b["type"] for b in content] == ["image_url", "text"]
        assert "no reference photo" in content[-1]["text"]

    async def test_it_sends_the_same_system_prompt_as_anthropic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One prompt, two providers. If they drift, a verdict stops meaning the
        same thing depending on which one answered."""
        captured: dict[str, object] = {}
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(monkeypatch, body=ok_body(), captured=captured)

        await ask()

        messages = captured["json"]["messages"]  # type: ignore[index]
        assert messages[0] == {"role": "system", "content": vision.SYSTEM}

    async def test_it_constrains_the_answer_to_the_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(monkeypatch, body=ok_body(), captured=captured)

        await ask()

        fmt = captured["json"]["response_format"]  # type: ignore[index]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True
        assert set(fmt["json_schema"]["schema"]["required"]) == {
            "verdict",
            "confidence",
            "rationale",
        }

    async def test_the_key_travels_as_a_bearer_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(monkeypatch, body=ok_body(), captured=captured)

        await ask()

        assert captured["auth"] == "Bearer test-key"
        assert captured["url"] == vision.GROQ_URL


class TestResult:
    async def test_it_records_the_model_that_answered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not the one that was asked for. A provider quietly serving something
        else has to be visible in the audit trail."""
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        body = ok_body()
        body["model"] = "qwen/qwen3.8-27b-0925"
        mock_groq(monkeypatch, body=body)

        result = await ask()

        assert result.model == "qwen/qwen3.8-27b-0925"
        assert result.prompt_version == vision.PROMPT_VERSION
        assert result.verdict == "fail"
        assert result.confidence == 0.9
        assert result.rationale == "Debris across the basin."
        assert result.compared_to_reference is False

    async def test_it_reports_having_compared_against_a_standard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(monkeypatch, body=ok_body())

        assert (await ask(reference=b"\xff\xd8\xffref")).compared_to_reference is True


class TestFailures:
    async def test_a_rate_limit_is_no_verdict_rather_than_a_wrong_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The free tier is 8000 tokens a minute and two photos is most of a
        request, so this happens in ordinary use."""
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(
            monkeypatch,
            status=429,
            body={"error": {"message": "Rate limit reached ... try again in 12s"}},
        )
        with pytest.raises(vision.VisionUnavailable, match="rate limit"):
            await ask()

    async def test_the_providers_own_message_survives_into_the_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It lands in job_runs.error_detail, where somebody has to read it."""
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(
            monkeypatch,
            status=400,
            body={"error": {"message": "model does not support images"}},
        )
        with pytest.raises(vision.VisionUnavailable, match="does not support images"):
            await ask()

    async def test_an_off_schema_answer_is_refused_rather_than_salvaged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(
            monkeypatch,
            body={"model": "m", "choices": [{"message": {"content": "looks fine to me"}}]},
        )
        with pytest.raises(vision.VisionUnavailable, match="nothing matching the schema"):
            await ask()

    async def test_an_empty_response_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(monkeypatch, body={"model": "m", "choices": []})
        with pytest.raises(vision.VisionUnavailable):
            await ask()

    async def test_a_confidence_outside_the_range_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """VisionVerdict bounds it 0-1. A model reporting 8.5 has not answered
        the question that was asked."""
        monkeypatch.setattr(vision, "get_settings", groq_settings)
        mock_groq(
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
        monkeypatch.setattr(vision, "get_settings", lambda: groq_settings(GROQ_API_KEY=""))
        with pytest.raises(vision.VisionUnavailable, match="GROQ_API_KEY"):
            await ask()


class TestProviderDispatch:
    def test_anthropic_is_the_declared_default(self) -> None:
        """A missing AI_REVIEW_PROVIDER must never route production traffic at
        the testing provider. Asserted against the declared default rather than
        a constructed Settings, which would read whatever .env happens to say."""
        assert Settings.model_fields["AI_REVIEW_PROVIDER"].default == "anthropic"

    async def test_the_default_takes_the_anthropic_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            vision,
            "get_settings",
            lambda: settings(ANTHROPIC_API_KEY="", GROQ_API_KEY="present"),
        )
        # Proven by which key it complains about — and it never reaches the
        # network, because the Anthropic path refuses before constructing a
        # client.
        with pytest.raises(vision.VisionUnavailable, match="ANTHROPIC_API_KEY"):
            await ask()

    async def test_groq_is_chosen_only_when_asked_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            vision,
            "get_settings",
            lambda: settings(AI_REVIEW_PROVIDER="groq", GROQ_API_KEY=""),
        )
        with pytest.raises(vision.VisionUnavailable, match="GROQ_API_KEY"):
            await ask()
