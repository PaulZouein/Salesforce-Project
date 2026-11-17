"""What an investigation costs: tokens, the estimated price, Claude Code's own total, the plan's limits."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from webapp.usage import PRICES, UsageBook, UsageMeter, price_for


def assistant(message_id: str, usage: dict, model: str = "claude-sonnet-5-5") -> dict:
    return {"type": "assistant", "message": {"id": message_id, "model": model, "usage": usage, "content": []}}


def usage(given: int, written: int, read: int, made: int, *, one_hour: bool = True) -> dict:
    split = {"ephemeral_1h_input_tokens": written, "ephemeral_5m_input_tokens": 0} if one_hour else \
        {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": written}
    return {"input_tokens": given, "cache_creation_input_tokens": written, "cache_read_input_tokens": read,
            "output_tokens": made, "cache_creation": split}


#: The shape of a recorded run (Sonnet 5.5): each content block of a response repeats its usage.
RECORDED = [
    assistant("msg_a", usage(2, 15606, 27331, 24)),
    assistant("msg_b", usage(4, 426, 42937, 32)),
    assistant("msg_b", usage(4, 426, 42937, 32)),
    assistant("msg_b", usage(4, 426, 42937, 32)),
    assistant("msg_c", usage(2, 1362, 43363, 8)),
    assistant("msg_c", usage(2, 1362, 43363, 8)),
    assistant("msg_d", usage(2, 135, 44725, 354)),
]
RECORDED_RESULT = {"type": "result", "subtype": "success", "total_cost_usd": 0.0934516, "modelUsage": {
    "claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 748, "cacheReadInputTokens": 158356,
                          "cacheCreationInputTokens": 17529, "costUSD": 0.0934516}}}


def test_the_estimate_counts_each_response_once_and_ends_on_claude_codes_own_total():
    meter = UsageMeter()
    assert meter.snapshot()["cost_usd"] == 0 and meter.used is False
    for message in RECORDED:
        assert meter.take(message) == "usage"
    live = meter.snapshot()
    # input 10 at $2, 1-hour cache writes 17,529 at 2 x $2, cache reads 158,356 at $0.10, output 418 at $10 (per million)
    expected = 10 * 2e-6 + 17529 * 4e-6 + 158356 * 0.1e-6 + 418 * 10e-6
    assert live["cost_usd"] == round(expected, 4) == 0.0902 and live["basis"] == "estimate"
    assert live["tokens"] == {"input": 10, "output": 418, "cache_read": 158356, "cache_write": 17529,
                              "total": 10 + 418 + 158356 + 17529}
    assert live["responses"] == 4 and live["models"] == ["claude-sonnet-5-5"]
    # Within a few percent of what Claude Code reported for that run; then its own figure is used.
    assert abs(live["cost_usd"] - 0.0934516) / 0.0934516 < 0.05
    meter.take(RECORDED_RESULT)
    final = meter.snapshot()
    assert final["cost_usd"] == 0.0935 and final["basis"] == "reported"
    assert final["tokens"]["output"] == 748 and final["tokens"]["total"] == 10 + 748 + 158356 + 17529


def test_cache_writes_are_priced_by_how_long_they_are_kept():
    one_hour, five_minutes = UsageMeter(), UsageMeter()
    one_hour.take(assistant("m", usage(0, 1_000_000, 0, 0, one_hour=True), model="claude-opus-5-5"))
    five_minutes.take(assistant("m", usage(0, 1_000_000, 0, 0, one_hour=False), model="claude-opus-5-5"))
    assert one_hour.snapshot()["cost_usd"] == 8.0 and five_minutes.snapshot()["cost_usd"] == 5.0


@pytest.mark.parametrize("model,expected", [
    ("claude-sonnet-5-5", (2.0, 10.0, 0.10)),
    ("claude-sonnet-5-5[1m]", (2.0, 10.0, 0.10)),
    ("claude-opus-5-5", (4.0, 20.0, 0.20)),
    ("claude-opus-5", (5.0, 25.0, 0.50)),
    ("claude-opus-5-20260301", (5.0, 25.0, 0.50)),
    ("claude-fable-5-2", (8.0, 40.0, 0.20)),
    ("claude-fable-5", (10.0, 50.0, 1.00)),
    ("CLAUDE-HAIKU-4-5", (1.0, 5.0, 0.10)),
])
def test_prices_per_model(model, expected):
    assert tuple(round(p * 1_000_000, 4) for p in price_for(model)) == expected


def test_prices_that_depend_on_prompt_size_or_speed_and_unknown_models():
    assert tuple(round(p * 1e6, 4) for p in price_for("claude-haiku-5-5", prompt_tokens=50_000)) == (0.10, 0.50, 0.01)
    assert tuple(round(p * 1e6, 4) for p in price_for("claude-haiku-5-5", prompt_tokens=150_000)) == (0.50, 2.50, 0.05)
    assert tuple(round(p * 1e6, 4) for p in price_for("claude-opus-5-5", fast=True)) == (8.0, 40.0, 0.40)
    assert price_for("claude-future-9") is None and price_for("") is None and price_for(None) is None
    meter = UsageMeter()
    meter.take(assistant("m", usage(100, 0, 0, 100), model="claude-future-9"))
    snap = meter.snapshot()
    assert snap["basis"] == "partial" and snap["unpriced_models"] == ["claude-future-9"] and snap["tokens"]["total"] == 200
    assert all(len(prices) == 3 for prices in PRICES.values())


def test_plan_figures_are_kept_as_numbers_and_states_only():
    meter = UsageMeter()
    event = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "resetsAt": 1794853200, "rateLimitType": "five_hour", "utilization": 0.92,
        "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled", "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": 0.92, "resetsAt": 1794853200, "status": "allowed_warning",
                                         "surpassedThreshold": 0.9},
                           "seven_day": {"utilization": 0.27, "resetsAt": 1794902400, "status": "allowed"}}}}
    assert meter.take(event) == "plan"
    assert meter.plan == {"status": "allowed_warning", "using_overage": False, "windows": {
        "five_hour": {"utilization": 0.92, "resetsAt": 1794853200, "status": "allowed_warning"},
        "seven_day": {"utilization": 0.27, "resetsAt": 1794902400, "status": "allowed"}}}
    # An event with a single window and no breakdown still counts; anything else is ignored.
    single = {"type": "rate_limit_event", "rate_limit_info": {"rateLimitType": "five_hour", "utilization": 0.5}}
    assert meter.take(single) == "plan" and meter.plan["windows"] == {"five_hour": {"utilization": 0.5}}
    for nothing in ({"type": "rate_limit_event"}, {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
                    {"type": "user"}, "text", None, {"type": "assistant", "message": {"content": []}}):
        assert meter.take(nothing) is None


def test_the_book_keeps_runs_and_totals_today_and_per_ticket(tmp_path):
    book = UsageBook(tmp_path)
    assert book.entries() == [] and book.today() == {"runs": 0, "cost_usd": 0, "tokens": 0} and book.load_plan() is None
    now = datetime.now(timezone.utc)
    stamp = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    book.record({"key": "A-1", "finished_at": stamp(now), "cost_usd": 0.5, "tokens": {"total": 1000}})
    book.record({"key": "A-1", "finished_at": stamp(now - timedelta(days=3)), "cost_usd": 2.0, "tokens": {"total": 9}})
    book.record({"key": "B-2", "finished_at": stamp(now), "cost_usd": 0.25, "tokens": {"total": 500}})
    with (tmp_path / "usage" / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not json\n[1, 2]\n")
    assert len(book.entries()) == 3
    assert book.today() == {"runs": 2, "cost_usd": 0.75, "tokens": 1500}
    assert book.for_ticket("A-1") == {"runs": 2, "cost_usd": 2.5, "tokens": 1009}
    book.save_plan({"status": "allowed", "windows": {"five_hour": {"utilization": 0.1}}})
    plan = book.load_plan()
    assert plan["windows"]["five_hour"]["utilization"] == 0.1 and plan["seen_at"].endswith("Z")
    assert json.loads((tmp_path / "usage" / "plan.json").read_text(encoding="utf-8"))["status"] == "allowed"
