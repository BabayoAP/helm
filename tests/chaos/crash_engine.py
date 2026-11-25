"""Engine process for the kill -9 chaos test (TEST-6).

Run: python crash_engine.py PORT DATA_DIR N_BARS
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

from helm.app.lifecycle import build_engine, recover
from helm.brokers.fake_exchange_rpc import FakeExchangeClient
from helm.core.config import RiskConfig
from helm.core.enums import Mode, OrderType, Side
from helm.core.models import Bar, OrderIntent, Quote
from helm.data.instruments import InstrumentRegistry
from helm.infra.clock import LiveClock
from helm.oms.journal import Journal
from helm.strategy.api import BaseStrategy, Context
from tests.factories import SYM, instrument

QTY = Decimal("0.1")


class Churn(BaseStrategy):
    """Alternates flat <-> long 0.1 every bar and leaves a far-away resting limit every 3rd bar."""

    id = "churn"

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]:
        pos = ctx.position(bar.symbol).qty
        out: list[OrderIntent] = []
        if pos == 0:
            out.append(
                OrderIntent(
                    strategy_id=self.id,
                    symbol=bar.symbol,
                    side=Side.BUY,
                    type=OrderType.MARKET,
                    qty=QTY,
                )
            )
        elif pos > 0:
            out.append(
                OrderIntent(
                    strategy_id=self.id,
                    symbol=bar.symbol,
                    side=Side.SELL,
                    type=OrderType.MARKET,
                    qty=pos,
                    reduce_only=True,
                )
            )
        if (bar.ts_open // 60) % 3 == 0:
            px = (bar.c * Decimal("0.98")).quantize(Decimal("0.01"))
            out.append(
                OrderIntent(
                    strategy_id=self.id,
                    symbol=bar.symbol,
                    side=Side.BUY,
                    type=OrderType.LIMIT,
                    qty=QTY,
                    limit_price=px,
                )
            )
        return out


def price(i: int) -> Decimal:
    return Decimal(100) + Decimal(i % 17) / 10


async def main(port: int, data_dir: Path, n_bars: int) -> None:
    clock = LiveClock()
    client = FakeExchangeClient("127.0.0.1", port, account_id="paper-chaos")
    journal = Journal(data_dir / "journal.sqlite")
    risk = RiskConfig(
        max_position_pct_equity=Decimal(100),
        max_order_notional_usd=Decimal(10**6),
        max_open_orders=10_000,
        max_orders_per_min=10_000,
        max_orders_per_min_global=10_000,
        daily_loss_limit_pct=Decimal(99),
        max_drawdown_pct=Decimal(99),
    )
    engine, previous = build_engine(
        mode=Mode.PAPER,
        account_id="paper-chaos",
        run_id="chaos001",
        clock=clock,
        broker=client,
        journal=journal,
        instruments=InstrumentRegistry([instrument(min_notional=Decimal(0))]),
        risk_config=risk,
        starting_equity=Decimal(100_000),
        code_fn=lambda: "code",
    )
    await client.poll()  # start the user stream at the venue's current end
    rep = await recover(engine, previous=previous)
    print(
        f"RECOVERY fills={rep.fills_from_venue} stale={rep.stale_pending_rejected} "
        f"adopted_states={rep.states_adopted} orders={rep.orders_recovered}",
        flush=True,
    )
    if engine.ks.state.value != "RUNNING":
        await engine.control(
            __import__("helm.core.events", fromlist=["ControlCommand"]).ControlCommand(
                action="resume", source="test", ts=clock.now()
            )
        )  # type: ignore[arg-type]
    engine.add_strategy(Churn(), symbols=(SYM,))
    progress = data_dir / "progress"
    start = int(progress.read_text()) + 1 if progress.exists() else 0
    for i in range(start, n_bars):
        px = price(i)
        await client.set_price(SYM, px, px + Decimal("0.01"))
        now = clock.now()
        await engine.on_quote(
            Quote(
                symbol=SYM,
                bid=px,
                bid_sz=Decimal(5),
                ask=px + Decimal("0.01"),
                ask_sz=Decimal(5),
                ts_event=now,
                ts_recv=now,
            )
        )
        bar = Bar(
            symbol=SYM,
            timeframe="1m",
            ts_open=i * 60,
            ts_close=i * 60 + 60,
            o=px,
            h=px + 1,
            l=px - 1,
            c=px,
            volume=Decimal(10),
        )
        await engine.on_bar(bar)
        for ev in await client.poll():
            await engine.on_broker_event(ev)
        progress.write_text(str(i))
        await asyncio.sleep(0.004)
    for ev in await client.poll():
        await engine.on_broker_event(ev)
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])))
