"""Live-mode triple gate (EXE-1) and broker construction (PAP-2).

Live broker construction requires ALL of:
  (a) env HELM_LIVE_TRADING=1
  (b) bot config mode = "live"
  (c) strategy lifecycle >= LIVE_SMALL (for real money: as recorded in the
      promotion registry for this exact code + params, decision 0030)
Any missing gate => LiveGateError naming the gate(s). Paper and backtest bots
always get a SimBroker, whatever the environment says.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from helm.brokers.base import Broker
from helm.brokers.sim.broker import SimBroker
from helm.core.clock import Clock
from helm.core.config import BotConfig
from helm.core.enums import LifecycleStage, Mode
from helm.core.errors import LiveGateError

LIVE_ENV = "HELM_LIVE_TRADING"
GATE_TOKEN = object()  # only this module can hand it to LiveBrokerBase

LiveFactory = Callable[[BotConfig, object], Broker]


def missing_live_gates(
    bot: BotConfig, env: Mapping[str, str], *, registry_stage: LifecycleStage | None = None
) -> list[str]:
    missing: list[str] = []
    if env.get(LIVE_ENV) != "1":
        missing.append(f"gate (a): environment {LIVE_ENV}=1 is not set")
    if bot.mode is not Mode.LIVE:
        missing.append(f"gate (b): bot {bot.id!r} config mode is {bot.mode.value!r}, not 'live'")
    if not bot.lifecycle.at_least(LifecycleStage.LIVE_SMALL):
        missing.append(
            f"gate (c): strategy lifecycle is {bot.lifecycle.value}, needs >= LIVE_SMALL"
        )
    elif registry_stage is not None and not registry_stage.at_least(LifecycleStage.LIVE_SMALL):
        missing.append(
            f"gate (c): promotion registry has this code+params at {registry_stage.value}, "
            "needs >= LIVE_SMALL (helm promote; decision 0030)"
        )
    return missing


def build_broker(
    bot: BotConfig,
    *,
    env: Mapping[str, str],
    clock: Clock,
    live_factory: LiveFactory | None = None,
    registry_stage: LifecycleStage | None = None,
    **sim_kwargs: Any,
) -> Broker:
    if bot.mode is not Mode.LIVE:
        return SimBroker(account_id=bot.account_id, venue=bot.venue, clock=clock, **sim_kwargs)
    missing = missing_live_gates(bot, env, registry_stage=registry_stage)
    if missing:
        raise LiveGateError("live broker refused: " + "; ".join(missing))
    if live_factory is None:
        raise LiveGateError(f"no live adapter registered for venue {bot.venue!r}")
    broker = live_factory(bot, GATE_TOKEN)
    if not broker.is_live or broker.account_id != bot.account_id:
        raise LiveGateError("live factory returned a broker for the wrong account")
    return broker
