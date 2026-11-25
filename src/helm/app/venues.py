"""Live venue factories, handed to `helm.app.gates.build_broker` (EXE-1).

The factory only runs after all three gates are open; the gate token it
receives is what lets the adapter be constructed at all.
"""

from __future__ import annotations

from helm.app.gates import LiveFactory
from helm.app.services import alpaca_credentials
from helm.brokers.base import Broker
from helm.brokers.live.alpaca import AlpacaBroker
from helm.core.clock import Clock
from helm.core.config import BotConfig, HelmConfig
from helm.core.errors import LiveGateError
from helm.infra.secrets import SecretStore
from helm.infra.ws import connect_ws


def live_factory(cfg: HelmConfig, store: SecretStore, clock: Clock) -> LiveFactory:
    def make(bot: BotConfig, gate_token: object) -> Broker:
        if bot.venue != "alpaca":
            raise LiveGateError(f"no live adapter for venue {bot.venue!r} (decision 0023)")
        venue = cfg.venues.alpaca
        return AlpacaBroker(
            account_id=bot.account_id,
            environment=venue.environment,
            credentials=alpaca_credentials(store, venue.credentials),
            clock=clock,
            ws_connect=connect_ws,
            _gate_token=gate_token,
        )

    return make
