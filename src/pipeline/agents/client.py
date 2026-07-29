"""LLM client wrapper + cost/spend accounting (docs/ROADMAP.md task 7.2).

One thin, INJECTABLE seam so the signal spine never imports this (I6) and tests
never hit the network: `LLMClient` is a Protocol; `AnthropicClient` is the real
impl (SDK-backed, fixed system prompt cached, structured JSON out); tests pass a
fake. Every call's token usage is turned into a cost and logged to `llm_spend`.

ROADMAP-NOTE: prices below are USD per million tokens and MUST be re-verified
against https://www.anthropic.com/pricing before trusting the cost dashboard —
they are best-effort as of the build date and flagged in the morning notes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from pipeline.common.models import LlmSpend
from pipeline.common.timeutil import utcnow

# Canonical model IDs (force-run selectable set) + friendly aliases.
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "opus-4-8": "claude-opus-4-8",
    "sonnet": "claude-sonnet-5",
    "sonnet-5": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
    "haiku-4-5": "claude-haiku-4-5-20251001",
}
ALLOWED_MODELS: frozenset[str] = frozenset(
    {"claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"}
)
DEFAULT_RANKER_MODEL = "claude-sonnet-5"
DEFAULT_ANALYST_MODEL = "claude-opus-4-8"

# (input, output) USD per 1M tokens. Cache write = 1.25x input, cache read = 0.1x.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
}
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


def resolve_model(name: str) -> str:
    """Map an alias or full id to a validated canonical model id."""
    resolved = MODEL_ALIASES.get(name.strip().lower(), name.strip())
    if resolved not in ALLOWED_MODELS:
        raise ValueError(
            f"model {name!r} not allowed; choose one of {sorted(ALLOWED_MODELS)} "
            f"(or aliases {sorted(MODEL_ALIASES)})"
        )
    return resolved


def is_opus(model: str) -> bool:
    """True for Opus-class models — the expensive tier that AUTOMATED runs must
    avoid (accepts an alias or full id)."""
    return resolve_model(model).startswith("claude-opus")


def compute_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """USD cost of one call from its token counts (cached input is cheaper)."""
    in_price, out_price = MODEL_PRICES.get(model, (0.0, 0.0))
    return round(
        (
            input_tokens * in_price
            + cache_creation_tokens * in_price * _CACHE_WRITE_MULT
            + cache_read_tokens * in_price * _CACHE_READ_MULT
            + output_tokens * out_price
        )
        / 1_000_000.0,
        6,
    )


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float


class LLMClient(Protocol):
    """The only surface the ranker/analyst depend on (keeps the SDK out of tests)."""

    def complete(
        self, *, system: str, user: str, model: str, max_tokens: int = 2048
    ) -> LLMResult: ...


class AnthropicClient:
    """Real client: SDK call with the fixed system prompt cached, JSON expected out."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set (I9: secrets via env only)")
        import anthropic  # imported lazily so tests never need the SDK

        self._client = anthropic.Anthropic(api_key=key)

    def complete(self, *, system: str, user: str, model: str, max_tokens: int = 2048) -> LLMResult:
        model = resolve_model(model)
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            # cache_control on the fixed system prompt -> cheap reads across candidates.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
        u = resp.usage
        cc = getattr(u, "cache_creation_input_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        return LLMResult(
            text=text,
            model=model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_tokens=cc,
            cache_read_tokens=cr,
            cost_usd=compute_cost(
                model,
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_creation_tokens=cc,
                cache_read_tokens=cr,
            ),
        )


class SubscriptionClient:
    """Claude-PLAN-backed client via the official Agent SDK — no API credits.

    Auth: `claude setup-token` (1-year token, user-created) -> CLAUDE_CODE_OAUTH_TOKEN
    env var (I9: secrets via env only). Usage draws from the subscription's rolling
    5-hour/weekly windows instead of metered credits, so cost_usd is logged as 0.0
    (with real token counts when the SDK reports them) — the $-soft-cap machinery
    stays inert for this backend by design; the plan's own limits are the cap.

    Policy note (verified against support article 15036540, 2026-07-23):
    subscription OAuth for the Agent SDK is currently supported, but Anthropic
    restricted it earlier in 2026 before reversing — if it flips again, set
    CLAUDE_API=true (or delete the line) and the factory falls back to the
    metered API client.

    The SDK is agent-first; we run it as a plain one-shot completion (no tools,
    single turn). max_tokens is advisory only — the SDK has no per-call output
    cap, so the models' own output limits (>= our 16K ceiling) apply.
    """

    def __init__(self, oauth_token: str | None = None) -> None:
        tok = oauth_token or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not tok:
            raise RuntimeError(
                "CLAUDE_CODE_OAUTH_TOKEN not set — run `claude setup-token` once and "
                "put the token in .env to use the subscription backend (I9)"
            )
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = tok
        try:
            import claude_agent_sdk  # noqa: F401 — lazy, so tests never need it
        except ImportError as exc:
            raise RuntimeError(
                "claude-agent-sdk not installed — pip install claude-agent-sdk"
            ) from exc

    async def _one_shot(self, system: str, user: str, model: str) -> tuple[str, dict]:
        from claude_agent_sdk import ClaudeAgentOptions, query

        opts = ClaudeAgentOptions(
            system_prompt=system, model=model, allowed_tools=[], max_turns=1
        )
        chunks: list[str] = []
        final: str | None = None
        usage: dict = {}
        async for message in query(prompt=user, options=opts):
            for block in getattr(message, "content", None) or []:
                text = getattr(block, "text", None)
                if text:
                    chunks.append(text)
            # ResultMessage: authoritative final text + usage, when present.
            final = getattr(message, "result", None) or final
            u = getattr(message, "usage", None)
            if u:
                usage = u if isinstance(u, dict) else dict(getattr(u, "__dict__", {}))
        return ("".join(chunks) or final or ""), usage

    def complete(self, *, system: str, user: str, model: str, max_tokens: int = 2048) -> LLMResult:
        import asyncio

        model = resolve_model(model)
        text, usage = asyncio.run(self._one_shot(system, user, model))
        return LLMResult(
            text=text,
            model=model,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cost_usd=0.0,  # subscription: no marginal dollar cost; plan limits govern
        )


def default_client() -> LLMClient:
    """The production client, switched by ONE env boolean:

        CLAUDE_API=true   -> AnthropicClient (metered API credits; the default)
        CLAUDE_API=false  -> SubscriptionClient (the user's Claude plan)

    Unset/unrecognized values mean true — the safe default is the backend that
    was always there. Flip to false when credits run dry; flip back (or delete
    the line) if the subscription route's policy ever changes."""
    flag = (os.environ.get("CLAUDE_API") or "true").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return SubscriptionClient()
    return AnthropicClient()


def log_spend(
    session: Session,
    result: LLMResult,
    *,
    purpose: str,
    run_id: str | None = None,
    ok: bool = True,
    now: datetime | None = None,
) -> LlmSpend:
    """Append one llm_spend row for a call (task 7.2 — spend logged per call)."""
    row = LlmSpend(
        created_at=now or utcnow(),
        purpose=purpose,
        model=result.model,
        run_id=run_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_creation_tokens=result.cache_creation_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cost_usd=result.cost_usd,
        ok=ok,
    )
    session.add(row)
    session.flush()
    return row


def default_daily_cap() -> float:
    """Conservative soft cap (USD/day); override with AGENT_DAILY_USD_CAP."""
    try:
        return float(os.environ.get("AGENT_DAILY_USD_CAP", "2.0"))
    except ValueError:
        return 2.0


def spend_since(session: Session, since: datetime) -> float:
    """Total logged USD spend at/after `since`."""
    from sqlalchemy import func, select

    total = session.execute(
        select(func.coalesce(func.sum(LlmSpend.cost_usd), 0.0)).where(LlmSpend.created_at >= since)
    ).scalar_one()
    return float(total or 0.0)


class SoftCapExceeded(RuntimeError):
    """Raised before a call when the day's logged spend is already at the soft cap."""


def enforce_daily_cap(
    session: Session, *, cap: float | None = None, now: datetime | None = None
) -> None:
    """Guard a run: refuse to start if today's spend already meets the cap."""
    now = now or utcnow()
    cap = cap if cap is not None else default_daily_cap()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    spent = spend_since(session, day_start)
    if spent >= cap:
        raise SoftCapExceeded(
            f"daily LLM soft cap reached: ${spent:.4f} >= ${cap:.2f} "
            f"(raise AGENT_DAILY_USD_CAP to override)"
        )
