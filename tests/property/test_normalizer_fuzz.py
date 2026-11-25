"""DATA-1: fuzzed venue payloads never crash the normalizer; models round-trip."""

from __future__ import annotations

import json
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from helm.core.events import decode_event, event_kind
from helm.core.models import Quote
from helm.data.normalizer import Normalizer

json_scalars = (
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=20)
)
json_values = st.recursive(
    json_scalars,
    lambda c: st.lists(c, max_size=6) | st.dictionaries(st.text(max_size=10), c, max_size=6),
    max_leaves=30,
)
channels = st.sampled_from(["ticker", "trade", "ohlcv", "nope", None])


@settings(max_examples=400, deadline=None)
@given(
    channel=channels,
    body=st.dictionaries(
        st.sampled_from(
            [
                "helm_symbol",
                "bid",
                "ask",
                "timestamp",
                "price",
                "amount",
                "id",
                "side",
                "ohlcv",
                "timeframe",
                "bidVolume",
            ]
        ),
        json_values,
        max_size=11,
    ),
)
def test_fuzzed_payloads_never_crash(channel: str | None, body: dict[str, object]) -> None:
    n = Normalizer()
    payload = {**body, "channel": channel}
    try:
        raw = json.dumps(payload)
    except ValueError:
        return
    out = n.normalize(raw, ts_recv=0)
    assert isinstance(out, list)
    assert sum(n.dropped.values()) + sum(n.ok.values()) >= 1 or out == []


@given(st.binary(max_size=64))
def test_garbage_bytes_dropped(raw: bytes) -> None:
    n = Normalizer()
    assert n.normalize(raw, 0) == [] or n.ok


def test_valid_payloads_parse() -> None:
    n = Normalizer()
    q = n.normalize(
        {
            "channel": "ticker",
            "helm_symbol": "x:A/B",
            "bid": 1.5,
            "ask": 1.6,
            "timestamp": 1000,
            "extra": 1,
        },
        5,
    )
    assert isinstance(q[0], Quote) and q[0].ts_event == 10**9 and q[0].bid == Decimal("1.5")
    t = n.normalize(
        {
            "channel": "trade",
            "helm_symbol": "x:A/B",
            "price": "2",
            "amount": "1",
            "id": 9,
            "side": "sell",
            "timestamp": 1,
        },
        5,
    )
    assert t[0].trade_id == "9"  # type: ignore[attr-defined]
    b = n.normalize(
        {
            "channel": "ohlcv",
            "helm_symbol": "x:A/B",
            "timeframe": "1m",
            "ohlcv": [0, 1, 2, 0.5, 1.5, 3],
        },
        60 * 10**9,
    )
    assert b[0].closed  # type: ignore[attr-defined]
    assert n.normalize("[1,2]", 0) == [] and n.dropped["?"] == 1


@given(
    bid=st.decimals(min_value=0, max_value=10**9, allow_nan=False, places=8),
    ts=st.integers(min_value=0, max_value=2**62),
)
def test_quote_roundtrip(bid: Decimal, ts: int) -> None:
    q = Quote(
        symbol="x:A/B",
        bid=bid,
        bid_sz=Decimal(1),
        ask=bid + 1,
        ask_sz=Decimal(1),
        ts_event=ts,
        ts_recv=ts,
    )
    assert decode_event(event_kind(q), q.model_dump_json()) == q
