"""Real-model drafting eval (gated).

Excluded from the default suite and CI (it costs API tokens) — run explicitly:

    uv run pytest -m eval          # requires ANTHROPIC_API_KEY (read from .env)

Guards the class of bug unit tests can't catch: prompt *adherence* — that the
model writes real content into the draft/summary field rather than echoing a
task label, and addresses the right recipient. See
docs/solutions/2026-06-01-real-model-prompt-adherence.md.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.drafting.voice import extract_ceo_exemplar
from app.models import Intent, ParsedRequest, ParsedThread, ResultKind

pytestmark = pytest.mark.eval

_THREAD = ParsedThread(
    original_sender="sarah@acme.com",
    recipients=["ceo@example.com"],
    subject="Partnership proposal",
    body=(
        "From: Sarah Lee <sarah@acme.com>\nTo: CEO <ceo@example.com>\n"
        "Subject: Partnership proposal\n\n"
        "Hi — we'd love to explore a partnership. Could you share a time to talk?"
    ),
)


@pytest.fixture
def service():
    settings = Settings(
        external_llm_ack=True,
        provider_api_key="x",
        webhook_basic_auth="y",
        postmark_sender="nora@example.com",
    )
    if not settings.anthropic_api_key:
        pytest.skip("no ANTHROPIC_API_KEY (set it in .env) — real-model eval skipped")
    import anthropic

    from app.drafting.claude import DraftingService

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=30.0)
    return DraftingService(client, settings.claude_model)


def test_draft_is_real_content_not_a_label(service) -> None:
    result = service.produce(ParsedRequest(thread=_THREAD), Intent.DRAFT)
    body = result.text.strip()
    assert result.kind is ResultKind.DRAFT
    assert len(body) > 40, f"draft too short / degenerate: {body!r}"
    assert body.upper() not in {"DRAFT", "SUMMARIZE"}
    assert body.split()[0].upper() not in {"DRAFT", "SUMMARIZE"}, "task label leaked into body"
    assert "[nora]" not in body.lower() and "as an ai" not in body.lower()
    assert result.interpretation_readback.strip()


def test_summary_is_real_content_not_a_label(service) -> None:
    result = service.produce(ParsedRequest(thread=_THREAD), Intent.SUMMARIZE)
    body = result.text.strip()
    assert result.kind is ResultKind.SUMMARY
    assert len(body) > 30, f"summary too short / degenerate: {body!r}"
    assert not body.upper().startswith(("DRAFT", "SUMMARIZE")), "task label leaked into summary"


# --- voice-fidelity A/B eval (U4) ------------------------------------------
#
# Measures the differentiator: does conditioning the draft on the CEO's own prior
# in-thread messages produce a better STYLE match than the generic baseline? Uses
# SYNTHETIC personas (no real PII). LLM-judge absolute scores are noisy, so the
# pass-bar is the A/B DELTA on the same judge/fixtures: exemplar >= generic - 1.
# See docs/plans/2026-06-12-002-feat-voice-fidelity-drafting-plan.md (U4).

VOICE_TOLERANCE = 1  # judge-noise slack on a 1-10 scale
VOICE_FLOOR = 3  # the exemplar variant must clear a basic style-match floor, not just win the delta
_JUDGE_MODEL = "claude-haiku-4-5"  # cheap model for style scoring (R3); drafts use the prod model

# Each persona: a forwarded thread whose top is a counterpart's new question, with
# the persona's own prior replies (distinctive voice) lower in the thread.
_PERSONA_DANA_EMAIL = "dana@startup.io"
_PERSONA_DANA_THREAD = """From: Priya <priya@vendor.com>
Subject: Re: launch

Quick one — do you want the press release reviewed before or after launch?

From: Dana <dana@startup.io>
Subject: Re: launch

yeah ship it friday. no need for a big rollout. keep it lean. —d

From: Priya <priya@vendor.com>
Subject: Re: launch

Should we do a phased rollout or a full launch?

From: Dana <dana@startup.io>
Subject: Re: launch

works for me. dont overthink it. —d
"""

_PERSONA_MARGARET_EMAIL = "m.ellsworth@university.edu"
_PERSONA_MARGARET_THREAD = """From: James Whitfield <j.whitfield@press.com>
Subject: Re: Manuscript schedule

Could you let me know your availability for the copy-edit review next month?

From: Margaret Ellsworth <m.ellsworth@university.edu>
Subject: Re: Manuscript schedule

Dear James,

Thank you so much for your patience. I would be delighted to review the chapters,
and I expect to have my comments to you by the end of the fortnight.

Warm regards,
Margaret

From: James Whitfield <j.whitfield@press.com>
Subject: Re: Manuscript schedule

Are the introductory chapters ready for proofing?

From: Margaret Ellsworth <m.ellsworth@university.edu>
Subject: Re: Manuscript schedule

Dear James,

They are very nearly complete; I shall forward them to you shortly.

Warm regards,
Margaret
"""

_PERSONAS = [
    ("dana", _PERSONA_DANA_EMAIL, _PERSONA_DANA_THREAD),
    ("margaret", _PERSONA_MARGARET_EMAIL, _PERSONA_MARGARET_THREAD),
]

_JUDGE_TOOL = {
    "name": "voice_score",
    "description": "Rate how well a candidate email reply matches a reference author's writing STYLE.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "description": "1 (no style match) to 10 (indistinguishable style match).",
            },
            "reason": {"type": "string"},
        },
        "required": ["score", "reason"],
    },
}

_JUDGE_SYSTEM = (
    "You are a forensic stylometry rater. Compare ONLY the writing STYLE of a candidate "
    "email reply to reference samples by the same author: tone/formality, greeting and "
    "sign-off, sentence length and rhythm, punctuation and casing. Ignore subject matter, "
    "factual correctness, and whether the reply is 'good'. Score how stylistically "
    "indistinguishable the candidate is from the reference."
)


def _judge_voice(client, model, reference: str, candidate: str) -> int:
    """Single LLM-judge call → integer style-match score (1-10)."""
    resp = client.messages.create(
        model=model,
        max_tokens=300,
        system=_JUDGE_SYSTEM,
        tools=[_JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "voice_score"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"REFERENCE SAMPLES (the author's own writing):\n{reference}\n\n"
                    f"CANDIDATE REPLY to rate:\n{candidate}"
                ),
            }
        ],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "voice_score":
            score = int(block.input["score"])
            assert 1 <= score <= 10, f"judge returned out-of-range score: {score}"
            return score
    raise AssertionError("judge did not return a voice_score tool call")


@pytest.fixture
def judge(service):
    """Reuse the service's real client, but score with a cheap model (R3)."""
    return service._client, _JUDGE_MODEL


@pytest.mark.parametrize("name,email,thread_body", _PERSONAS, ids=[p[0] for p in _PERSONAS])
def test_exemplar_beats_generic_on_voice_match(service, judge, name, email, thread_body) -> None:
    client, model = judge
    exemplar = extract_ceo_exemplar(thread_body, {email})
    assert exemplar, f"fixture {name}: expected a non-empty CEO exemplar"

    thread = ParsedThread(body=thread_body)
    draft_a = service.produce(
        ParsedRequest(thread=thread, voice_exemplar=exemplar), Intent.DRAFT
    ).text.strip()
    draft_b = service.produce(
        ParsedRequest(thread=thread, voice_exemplar=""), Intent.DRAFT
    ).text.strip()

    # Both variants must be non-degenerate so the A/B can't pass on two bad drafts.
    for label, body in (("exemplar", draft_a), ("generic", draft_b)):
        assert len(body) > 40, f"{name}/{label} draft too short: {body!r}"
        assert "as an ai" not in body.lower(), f"{name}/{label} leaked AI disclaimer"

    score_a = _judge_voice(client, model, exemplar, draft_a)
    score_b = _judge_voice(client, model, exemplar, draft_b)

    # Absolute floor: guards against both variants scoring at the bottom (a passing
    # delta on two stylistically flat drafts would be meaningless).
    assert score_a >= VOICE_FLOOR, (
        f"{name}: exemplar-conditioned draft scored below the voice-match floor "
        f"(exemplar={score_a}, floor={VOICE_FLOOR})"
    )
    # Delta pass-bar: exemplar-conditioned draft scores at least as high as generic.
    assert score_a >= score_b - VOICE_TOLERANCE, (
        f"{name}: exemplar variant did not match the author's voice better than generic "
        f"(exemplar={score_a}, generic={score_b}, tolerance={VOICE_TOLERANCE})"
    )


def test_generic_fallback_produces_valid_draft(service) -> None:
    # A thread with no prior message from the queried address → empty exemplar →
    # the draft path still degrades gracefully to a valid generic draft.
    thread_body = _PERSONA_DANA_THREAD
    assert extract_ceo_exemplar(thread_body, {"stranger@nowhere.com"}) == ""
    result = service.produce(ParsedRequest(thread=ParsedThread(body=thread_body)), Intent.DRAFT)
    assert result.kind is ResultKind.DRAFT
    assert len(result.text.strip()) > 40


# --- Nora-voice meeting-proposal eval (proposal voice) ---------------------
#
# The proposal email reaches real third parties (0.2.0). Verifies the produced
# proposal clears hard guardrails (self-IDs as Nora, signs as Nora, no AI-disclaimer)
# and reads well (holistic judge floor). SYNTHETIC personas; no real PII.
# See docs/plans/2026-06-15-002-feat-proposal-voice-eval-plan.md.

PROPOSAL_FLOOR_EACH = 6   # every persona must clear this (judge-noise-tolerant)
PROPOSAL_FLOOR_AVG = 7    # the average must clear this higher bar

# (name, target_email, ceo_instruction, thread_body)
_PROPOSAL_PERSONAS = [
    (
        "vendor",
        "sarah@acme.com",
        "Set up a 30-minute intro call next week.",
        "From: Sarah Lee <sarah@acme.com>\nTo: CEO <ceo@example.com>\n"
        "Subject: Partnership\n\nHi — we'd love to explore a partnership. "
        "Could we find time to talk?",
    ),
    (
        "candidate",
        "jordan@inbox.com",
        "Propose a 45-minute interview, offer two slots this Thursday or Friday.",
        "From: Jordan Patel <jordan@inbox.com>\nTo: CEO <ceo@example.com>\n"
        "Subject: Staff engineer role\n\nThanks for considering me — happy to chat "
        "whenever works for your team.",
    ),
    (
        "partner",
        "lin@globex.com",
        "Suggest a quarterly sync, 30 minutes, sometime the week of the 23rd.",
        "From: Lin Zhao <lin@globex.com>\nTo: CEO <ceo@example.com>\n"
        "Subject: Q3 planning\n\nShould we get our teams aligned before Q3 kicks off?",
    ),
]


def _produce_proposal(service, target_email, ceo_instruction, thread_body):
    thread = ParsedThread(body=thread_body, original_sender=target_email)
    req = ParsedRequest(thread=thread, instruction_text=ceo_instruction)
    return service.produce(req, Intent.PROPOSE_MEETING)


@pytest.mark.parametrize(
    "name,target_email,ceo_instruction,thread_body",
    _PROPOSAL_PERSONAS, ids=[p[0] for p in _PROPOSAL_PERSONAS],
)
def test_proposal_clears_output_guards(service, name, target_email, ceo_instruction, thread_body) -> None:
    result = _produce_proposal(service, target_email, ceo_instruction, thread_body)
    assert result.kind is ResultKind.PROPOSAL, f"{name}: expected a PROPOSAL result"
    body = result.text.strip()
    low = body.lower()
    assert len(body) > 40, f"{name}: proposal too short / degenerate: {body!r}"
    assert "nora" in low, f"{name}: proposal does not self-identify as Nora"
    tail = "\n".join(body.strip().splitlines()[-4:]).lower()
    assert "nora" in tail, f"{name}: proposal does not sign off as Nora"
    assert "as an ai" not in low and "language model" not in low, f"{name}: AI-disclaimer leaked"


# --- holistic Nora-voice proposal judge (proposal voice) -------------------

_PROPOSAL_JUDGE_TOOL = {
    "name": "proposal_score",
    "description": "Rate how well a candidate email is a strong meeting-proposal sent by "
    "an executive assistant on the CEO's behalf.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "description": "1 (poor) to 10 (excellent) overall quality as a Nora-voice "
                "meeting proposal.",
            },
            "reason": {"type": "string"},
        },
        "required": ["score", "reason"],
    },
}

_PROPOSAL_JUDGE_SYSTEM = (
    "You rate an email that Nora, an executive assistant, sends to a third party on the "
    "CEO's behalf to propose a meeting. Score 1-10 on overall quality, rewarding: clearly "
    "introduces herself as the CEO's assistant; writes on the CEO's behalf and does NOT "
    "impersonate or write as the CEO; proposes a meeting with concrete time(s) and asks "
    "the recipient's availability; professional, warm, and appropriately concise; signs as "
    "Nora. Penalize: writing as if from the CEO, missing the meeting ask, no times, robotic "
    "or rambling tone, or any AI/system disclaimer."
)


def _judge_proposal(client, model, candidate: str) -> tuple[int, str]:
    resp = client.messages.create(
        model=model, max_tokens=300, system=_PROPOSAL_JUDGE_SYSTEM,
        tools=[_PROPOSAL_JUDGE_TOOL], tool_choice={"type": "tool", "name": "proposal_score"},
        messages=[{"role": "user", "content": f"CANDIDATE PROPOSAL to rate:\n{candidate}"}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "proposal_score":
            score = int(block.input["score"])
            assert 1 <= score <= 10, f"judge returned out-of-range score: {score}"
            return score, str(block.input.get("reason", ""))
    raise AssertionError("judge did not return a proposal_score tool call")


def test_proposal_voice_clears_judge_floor(service, judge) -> None:
    client, model = judge
    scores: list[int] = []
    for name, target_email, ceo_instruction, thread_body in _PROPOSAL_PERSONAS:
        body = _produce_proposal(service, target_email, ceo_instruction, thread_body).text.strip()
        score, reason = _judge_proposal(client, model, body)
        assert score >= PROPOSAL_FLOOR_EACH, (
            f"{name}: proposal scored {score} < per-item floor {PROPOSAL_FLOOR_EACH}. "
            f"judge reason: {reason}"
        )
        scores.append(score)
    avg = sum(scores) / len(scores)
    assert avg >= PROPOSAL_FLOOR_AVG, (
        f"proposal voice average {avg:.1f} < floor {PROPOSAL_FLOOR_AVG} (scores={scores})"
    )


def test_judge_penalizes_ceo_impersonation(service, judge) -> None:
    # Negative control: prove the judge is a real discriminator, not a rubber-stamp.
    # A proposal written AS the CEO (first person, signed by the CEO, no Nora) must
    # score below the per-item floor — otherwise the floor is meaningless.
    client, model = judge
    impersonating = (
        "Hi Sarah,\n\nI'd like to set up a 30-minute call next week to discuss the "
        "partnership. Let me know what times work for you.\n\nBest,\nAlex Chen\nCEO, Acme"
    )
    score, reason = _judge_proposal(client, model, impersonating)
    assert score < PROPOSAL_FLOOR_EACH, (
        f"judge did NOT penalize a CEO-impersonating proposal: scored {score} "
        f">= floor {PROPOSAL_FLOOR_EACH} (reason: {reason}). The judge is too lenient."
    )


# --- Ollama local-backend integration (gated) --------------------------------
#
# Runs a real local draft only if Ollama is reachable on localhost; otherwise
# skips gracefully. Gated under `pytestmark = pytest.mark.eval` at module level.


def test_ollama_backend_produces_a_real_draft() -> None:
    import httpx

    from app.drafting.ollama import OllamaDraftingService
    from app.models import Intent, ParsedRequest, ParsedThread, ResultKind

    import os

    import pytest

    base = os.environ.get("NORA_OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        tags = httpx.get(base + "/api/tags", timeout=3).json().get("models", [])
    except Exception:
        pytest.skip("no local Ollama reachable — skipping local-backend integration test")
    available = [m.get("name", "") for m in tags]
    if not available:
        pytest.skip("Ollama reachable but no models pulled")
    # Prefer the configured model if present; otherwise use whatever is available so the
    # test runs on any box (the Mini has qwen3:8b; a dev box may have something else).
    preferred = os.environ.get("NORA_OLLAMA_MODEL", "qwen3:8b")
    model = preferred if preferred in available else available[0]
    # Generous timeout: a CPU-only/Intel box drafts an 8B model far slower than the M4 Mini.
    svc = OllamaDraftingService(base, model, timeout=float(os.environ.get("NORA_OLLAMA_TIMEOUT", "300")))
    thread = ParsedThread(
        original_sender="sarah@acme.com",
        body="From: Sarah <sarah@acme.com>\n\nHi — can you send the Q3 numbers?",
    )
    result = svc.produce(ParsedRequest(thread=thread), Intent.DRAFT)
    assert result.kind is ResultKind.DRAFT
    assert len(result.text.strip()) > 20
    assert result.interpretation_readback.strip()
