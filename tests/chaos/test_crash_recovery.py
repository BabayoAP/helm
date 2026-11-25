"""TEST-6 / EXE-3: kill -9 the engine during active orders, restart, and verify:
no duplicate orders, no orphaned positions, internal state == venue state."""

from __future__ import annotations

import asyncio
import os
import random
import signal
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from helm.app.lifecycle import UnexpectedStateError, build_engine, recover
from helm.brokers.fake_exchange import FakeExchange, FakeExchangeConfig
from helm.brokers.fake_exchange_rpc import FakeExchangeClient, serve
from helm.core.config import RiskConfig
from helm.core.enums import Mode
from helm.data.instruments import InstrumentRegistry
from helm.infra.clock import LiveClock
from helm.oms.journal import Journal
from tests.factories import SYM, instrument

ROOT = Path(__file__).resolve().parents[2]
N_BARS = 4000


class VenueThread:
    def __init__(self, fx: FakeExchange) -> None:
        self.fx = fx
        self.loop = asyncio.new_event_loop()
        self.port = 0
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            server = self.loop.run_until_complete(serve(fx))
            self.port = server.sockets[0].getsockname()[1]
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(5)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


def _spawn(port: int, data: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tests" / "chaos" / "crash_engine.py"),
            str(port),
            str(data),
            str(N_BARS),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.chaos
def test_kill_dash_nine_recovery(tmp_path: Path) -> None:
    fx = FakeExchange(
        account_id="paper-chaos",
        config=FakeExchangeConfig(
            starting_cash=Decimal(100_000), partial_fill_parts=2, seed=3, latency_s=0.004
        ),
    )
    venue = VenueThread(fx)
    rng = random.Random(9)
    kills = 0
    recovered: list[str] = []
    try:
        inflight = 0
        while kills < 25 and (kills < 4 or inflight == 0):
            p = _spawn(venue.port, tmp_path)
            time.sleep(rng.uniform(0.9, 1.8))
            if p.poll() is None:
                os.kill(p.pid, signal.SIGKILL)
                kills += 1
            out, err = p.communicate(timeout=30)
            assert "Traceback" not in err, err
            lines = [ln for ln in out.splitlines() if ln.startswith("RECOVERY")]
            recovered += lines
            inflight += sum(
                int(ln.split("fills=")[1].split()[0]) + int(ln.split("stale=")[1].split()[0])
                for ln in lines
            )
        p = _spawn(venue.port, tmp_path)
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0 and "DONE" in out, err[-3000:]
    finally:
        venue.stop()
    assert kills >= 3
    # restarts really recovered mid-flight state (orders journaled before the kill)
    orders = [int(ln.split("orders=")[1]) for ln in recovered]
    assert len(recovered) >= 3 and max(orders) > 0
    assert inflight > 0, "no kill landed mid-flight (fills during downtime or stale pending orders)"
    print("\n" + "\n".join(recovered))
    # 0 duplicate orders: every client_order_id reached the venue exactly once
    assert fx.submit_calls and max(fx.submit_calls.values()) == 1
    assert all(n == 1 for n in fx.accepted_submits.values())
    # strategy invariant: market lots alternate flat/long, so a double-submit would show up here
    venue_pos = {p.symbol: p.qty for p in asyncio.run(fx.positions())}
    assert venue_pos.get(SYM, Decimal(0)) in (
        Decimal(0),
        Decimal("0.1"),
        Decimal("0.2"),
        Decimal("0.3"),
    )

    # state fully recovered: a fresh engine rebuilt from the journal matches the venue exactly
    async def final_check() -> None:
        v2 = VenueThread(fx)
        client = FakeExchangeClient("127.0.0.1", v2.port, account_id="paper-chaos")
        journal = Journal(tmp_path / "journal.sqlite")
        engine, prev = build_engine(
            mode=Mode.PAPER,
            account_id="paper-chaos",
            run_id="x",
            clock=LiveClock(),
            broker=client,
            journal=journal,
            instruments=InstrumentRegistry([instrument(min_notional=Decimal(0))]),
            risk_config=RiskConfig(),
            starting_equity=Decimal(100_000),
            code_fn=lambda: "c",
        )
        rep = await recover(engine, previous=prev)  # raises UnexpectedStateError on any orphan
        assert rep.unexpected == []
        ours = {s: p.qty for s, p in engine.portfolio.positions.items() if p.qty != 0}
        theirs = {p.symbol: p.qty for p in await client.positions()}
        assert ours == theirs
        assert {o.client_order_id for o in engine.om.open_orders()} == {
            o.client_order_id for o in await client.open_orders()
        }
        await client.close()
        journal.close()
        v2.stop()

    asyncio.run(final_check())


def test_unexplained_venue_state_refuses_then_adopt_or_flatten(tmp_path: Path) -> None:
    async def run() -> None:
        fx = FakeExchange(account_id="paper-x", config=FakeExchangeConfig())
        fx.set_price(SYM, Decimal(100), Decimal("100.01"))
        fx.inject_position(SYM, Decimal(2), Decimal(100))  # someone traded the account by hand

        def engine_for(j: Journal):  # type: ignore[no-untyped-def]
            return build_engine(
                mode=Mode.PAPER,
                account_id="paper-x",
                run_id="r",
                clock=LiveClock(),
                broker=fx,
                journal=j,
                instruments=InstrumentRegistry([instrument()]),
                risk_config=RiskConfig(),
                starting_equity=Decimal(10_000),
                code_fn=lambda: "c",
            )

        j = Journal(tmp_path / "a.sqlite")
        engine, prev = engine_for(j)
        with pytest.raises(UnexpectedStateError, match="refusing to trade"):
            await recover(engine, previous=prev)
        rep = await recover(engine, adopt=True, previous=prev)
        assert rep.adopted and engine.portfolio.position(SYM).qty == 2
        j.close()
        j2 = Journal(tmp_path / "a.sqlite")
        engine2, prev2 = engine_for(j2)
        assert (
            engine2.portfolio.position(SYM).qty == 2
        )  # adoption is journaled and survives restart
        assert (await recover(engine2, previous=prev2)).unexpected == []
        j2.close()

        fx.inject_position(SYM, Decimal(3), Decimal(100))
        j3 = Journal(tmp_path / "b.sqlite")
        engine3, prev3 = engine_for(j3)
        rep3 = await recover(engine3, flatten_first=True, previous=prev3)
        assert rep3.flattened and await fx.positions() == []
        assert engine3.portfolio.position(SYM).qty == 0
        j3.close()

    asyncio.run(run())
