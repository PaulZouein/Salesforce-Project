"""What an investigation costs: tokens, an estimated price, and the plan limits Claude Code reports.

While Claude Code works, every model response in its progress stream carries the tokens it used.
They are added up here and priced at Anthropic's API list prices, so the page can show a figure
that grows while the investigation runs. When the run ends, Claude Code reports its own total,
which then replaces the estimate.

Claude Code also passes on the account's plan usage (the share of the 5-hour and weekly limits
already used) when the account has such limits. That is kept as reported.

None of this is a bill. With a Claude subscription, runs count toward the plan's limits instead
of being charged per token; with an API key, the price is what the API charges at list prices.

The ledger (``usage/ledger.jsonl``, one line per finished run) and the last plan figures
(``usage/plan.json``) stay on this computer. They hold ticket keys and numbers, nothing else.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USAGE_DIRNAME = "usage"
MAX_LEDGER_LINES = 20_000

#: USD per million tokens: (input, output, cache read). Anthropic API list prices, November 2026.
#: Writing to the cache costs 1.25x the input price for the 5-minute cache and 2x for the 1-hour cache.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-mythos-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-mythos-5": (10.0, 50.0, 1.00),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
#: Models whose price changes above a prompt size: model -> (prompt tokens, prices above it).
LONG_PROMPT_PRICES: dict[str, tuple[int, tuple[float, float, float]]] = {
    "claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05)),
}
CACHE_WRITE_5M, CACHE_WRITE_1H, FAST_MODE = 1.25, 2.0, 2.0

PRICING_NOTE = ("Estimated from the tokens Claude Code reports, at Anthropic's API list prices; when a run ends, "
                "Claude Code's own total replaces the estimate. With a Claude subscription you are not charged per "
                "token: runs count toward your plan's limits, shown under Plan.")

_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")
_PLAN_FIELDS = ("utilization", "resetsAt", "status")


def price_for(model: Any, *, prompt_tokens: int = 0, fast: bool = False) -> tuple[float, float, float] | None:
    """``(input, output, cache read)`` in USD per token, or ``None`` for a model without a known price."""
    name = _SUFFIX_RE.sub("", str(model or "")).strip().lower()
    for key in sorted(PRICES, key=len, reverse=True):
        if name == key or name.startswith((key + "-", key + "@")):
            prices = PRICES[key]
            if key in LONG_PROMPT_PRICES and prompt_tokens > LONG_PROMPT_PRICES[key][0]:
                prices = LONG_PROMPT_PRICES[key][1]
            factor = (FAST_MODE if fast else 1.0) / 1_000_000
            return tuple(p * factor for p in prices)  # type: ignore[return-value]
    return None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _count(usage: dict[str, Any]) -> dict[str, int]:
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    written = _int(usage.get("cache_creation_input_tokens"))
    one_hour = _int(split.get("ephemeral_1h_input_tokens"))
    return {"input": _int(usage.get("input_tokens")), "output": _int(usage.get("output_tokens")),
            "cache_read": _int(usage.get("cache_read_input_tokens")), "cache_write": written,
            "cache_write_1h": min(one_hour, written)}


def _plan_from(info: Any) -> dict[str, Any] | None:
    """The plan figures of a ``rate_limit_event``, reduced to numbers and states."""
    if not isinstance(info, dict):
        return None
    windows = {}
    for name, window in (info.get("unifiedWindows") or {}).items():
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
            windows[str(name)[:40]] = {k: window[k] for k in _PLAN_FIELDS if isinstance(window.get(k), (int, float, str))}
    if not windows and isinstance(info.get("utilization"), (int, float)) and info.get("rateLimitType"):
        windows[str(info["rateLimitType"])[:40]] = {k: info[k] for k in _PLAN_FIELDS if k in info}
    if not windows:
        return None
    return {"status": str(info.get("status") or "")[:40] or None, "windows": windows,
            "using_overage": bool(info.get("isUsingOverage"))}


class UsageMeter:
    """Adds up what one run of Claude Code uses, from its progress stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: dict[str, tuple[str, dict[str, int], bool]] = {}
        self._reported: dict[str, Any] | None = None
        self.plan: dict[str, Any] | None = None

    def take(self, message: Any) -> str | None:
        """Read one stream message. Returns ``"usage"`` or ``"plan"`` when it changed something."""
        if not isinstance(message, dict):
            return None
        kind = message.get("type")
        if kind == "assistant":
            inner = message.get("message") if isinstance(message.get("message"), dict) else {}
            usage, key = inner.get("usage"), inner.get("id")
            if isinstance(usage, dict) and isinstance(key, str):
                # Each content block of a response repeats the response's usage: count a response once.
                with self._lock:
                    self._messages[key] = (str(inner.get("model") or ""), _count(usage), usage.get("speed") == "fast")
                return "usage"
        elif kind == "result":
            models = message.get("modelUsage") if isinstance(message.get("modelUsage"), dict) else {}
            tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
            for figures in models.values():
                if isinstance(figures, dict):
                    tokens["input"] += _int(figures.get("inputTokens"))
                    tokens["output"] += _int(figures.get("outputTokens"))
                    tokens["cache_read"] += _int(figures.get("cacheReadInputTokens"))
                    tokens["cache_write"] += _int(figures.get("cacheCreationInputTokens"))
            cost = message.get("total_cost_usd")
            with self._lock:
                self._reported = {"cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
                                  "tokens": tokens if any(tokens.values()) else None, "models": sorted(models)}
            return "usage"
        elif kind == "rate_limit_event":
            plan = _plan_from(message.get("rate_limit_info"))
            if plan:
                self.plan = plan
                return "plan"
        return None

    def snapshot(self) -> dict[str, Any]:
        """Tokens and price so far: an estimate while running, Claude Code's own total once it reported one."""
        with self._lock:
            messages = list(self._messages.values())
            reported = dict(self._reported) if self._reported else None
        tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        cost, models, unpriced = 0.0, set(), set()
        for model, counted, fast in messages:
            for name in tokens:
                tokens[name] += counted[name]
            if model:
                models.add(model)
            prices = price_for(model, prompt_tokens=counted["input"] + counted["cache_read"] + counted["cache_write"],
                               fast=fast)
            if prices is None:
                unpriced.add(model or "unknown")
                continue
            given, made, read = prices
            five_minute = counted["cache_write"] - counted["cache_write_1h"]
            cost += (counted["input"] * given + counted["output"] * made + counted["cache_read"] * read
                     + five_minute * given * CACHE_WRITE_5M + counted["cache_write_1h"] * given * CACHE_WRITE_1H)
        basis = "partial" if unpriced else "estimate"
        if reported and reported.get("tokens"):
            tokens = reported["tokens"]  # includes what subagents used
        if reported and reported.get("cost_usd") is not None:
            cost, basis, unpriced = reported["cost_usd"], "reported", set()
            models |= set(reported.get("models") or [])
        return {"tokens": {**tokens, "total": sum(tokens.values())}, "cost_usd": round(cost, 4), "basis": basis,
                "models": sorted(models), "unpriced_models": sorted(unpriced), "responses": len(messages)}

    @property
    def used(self) -> bool:
        return bool(self._messages or self._reported)


def _local_day(iso: Any) -> str | None:
    try:
        moment = datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return moment.astimezone().date().isoformat()


class UsageBook:
    """What runs have used on this computer, kept in ``usage/``."""

    def __init__(self, root: Path):
        self.dir = Path(root) / USAGE_DIRNAME
        self._lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True)
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                with (self.dir / "ledger.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass  # the meter must never stop an investigation

    def entries(self) -> list[dict[str, Any]]:
        try:
            lines = (self.dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-MAX_LEDGER_LINES:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
        return out

    @staticmethod
    def _total(entries: list[dict[str, Any]]) -> dict[str, Any]:
        cost = sum(float(e.get("cost_usd") or 0) for e in entries)
        tokens = sum(int((e.get("tokens") or {}).get("total") or 0) for e in entries)
        return {"runs": len(entries), "cost_usd": round(cost, 4), "tokens": tokens}

    def today(self, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        day = datetime.now().astimezone().date().isoformat()
        entries = self.entries() if entries is None else entries
        return self._total([e for e in entries if _local_day(e.get("finished_at")) == day])

    def for_ticket(self, key: str, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        entries = self.entries() if entries is None else entries
        return self._total([e for e in entries if e.get("key") == key])

    def save_plan(self, plan: dict[str, Any]) -> None:
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                temporary = self.dir / "plan.json.tmp"
                temporary.write_text(json.dumps({**plan, "seen_at": _utc_now()}, indent=2), encoding="utf-8")
                temporary.replace(self.dir / "plan.json")
            except OSError:
                pass

    def load_plan(self) -> dict[str, Any] | None:
        try:
            data = json.loads((self.dir / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("windows"), dict) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
