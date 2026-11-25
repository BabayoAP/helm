"""Typed configuration models (Appendix A). Loading from TOML lives in helm.infra.config.

Risk config lives in core so `risk` can import it while importing only `core`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from helm.core.enums import LifecycleStage, Mode

PAPER_ACCOUNT_PREFIX = "paper-"


class _Cfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DailyReset(_Cfg):
    tz: str = "UTC"
    time: str = "00:00"


class RiskConfig(_Cfg):
    max_position_pct_equity: Decimal = Field(default=Decimal("10.0"), gt=0)
    max_gross_leverage: Decimal = Field(default=Decimal("2.0"), gt=0)
    max_order_notional_usd: Decimal = Field(default=Decimal("500"), gt=0)
    fat_finger_pct: Decimal = Field(default=Decimal("3.0"), gt=0)
    max_orders_per_min: int = Field(default=30, gt=0)
    max_orders_per_min_global: int = Field(default=60, gt=0)
    max_open_orders: int = Field(default=20, gt=0)
    max_consecutive_rejects: int = Field(default=5, gt=0)
    stale_feed_ms: int = Field(default=2000, gt=0)
    feed_healthy_clear_s: int = Field(default=10, ge=0)
    clock_skew_warn_ms: int = 250
    clock_skew_pause_ms: int = 1000
    daily_loss_limit_pct: Decimal = Field(default=Decimal("3.0"), gt=0)
    max_drawdown_pct: Decimal = Field(default=Decimal("10.0"), gt=0)
    on_daily_loss: Literal["flatten_and_halt", "cancel_and_halt"] = "flatten_and_halt"
    daily_reset: DailyReset = DailyReset()
    reconcile_interval_s: int = 60
    reconcile_tolerance_pct: Decimal = Decimal("0.1")
    live_small_capital_pct: Decimal = Field(default=Decimal("2.0"), gt=0)
    live_small_max_exposure_usd: Decimal = Field(default=Decimal("500"), gt=0)
    max_strategy_capital_pct: Decimal = Field(default=Decimal("100"), gt=0)  # RISK-8 (Phase 3)
    symbol_allowlist: tuple[str, ...] = ()  # empty = any instrument with metadata

    def tightening_violations(self, new: RiskConfig) -> list[str]:
        """Fields where `new` is looser than self (EXE-7: loosen only by restart)."""
        looser: list[str] = []
        lower_is_tighter = (
            "max_position_pct_equity",
            "max_gross_leverage",
            "max_order_notional_usd",
            "fat_finger_pct",
            "max_orders_per_min",
            "max_orders_per_min_global",
            "max_open_orders",
            "max_consecutive_rejects",
            "stale_feed_ms",
            "daily_loss_limit_pct",
            "max_drawdown_pct",
            "live_small_capital_pct",
            "live_small_max_exposure_usd",
            "max_strategy_capital_pct",
            "reconcile_tolerance_pct",
            "clock_skew_warn_ms",
            "clock_skew_pause_ms",
        )
        for name in lower_is_tighter:
            if getattr(new, name) > getattr(self, name):
                looser.append(name)
        if new.feed_healthy_clear_s < self.feed_healthy_clear_s:
            looser.append("feed_healthy_clear_s")
        if self.symbol_allowlist and not set(new.symbol_allowlist) <= set(self.symbol_allowlist):
            looser.append("symbol_allowlist")
        if self.symbol_allowlist and not new.symbol_allowlist:
            looser.append("symbol_allowlist")
        if self.on_daily_loss == "flatten_and_halt" and new.on_daily_loss != "flatten_and_halt":
            looser.append("on_daily_loss")
        return sorted(set(looser))


class SecurityConfig(_Cfg):
    attest_no_withdrawal: bool = False
    telegram_allowed_user_ids: tuple[int, ...] = ()
    telegram_allowed_chat_ids: tuple[int, ...] = ()


class BotConfig(_Cfg):
    id: str
    strategy: str  # dotted path "helm.strategy.examples.sma_cross:SmaCross"
    mode: Mode = Mode.PAPER
    account_id: str
    venue: str
    symbols: tuple[str, ...]
    timeframe: str = "1m"
    params: dict[str, float | int | str | bool] = Field(default_factory=lambda: {})
    lifecycle: LifecycleStage = LifecycleStage.DRAFT

    @model_validator(mode="after")
    def _account_matches_mode(self) -> BotConfig:
        """PAP-2: paper accounts are named paper-*, and only paper accounts may be."""
        is_paper_acct = self.account_id.startswith(PAPER_ACCOUNT_PREFIX)
        if self.mode is Mode.LIVE and is_paper_acct:
            raise ValueError("live bots cannot use a paper-* account_id")
        if self.mode is not Mode.LIVE and not is_paper_acct:
            raise ValueError(
                f"{self.mode.value} bots must use a '{PAPER_ACCOUNT_PREFIX}*' account_id"
            )
        return self


class EngineConfig(_Cfg):
    data_dir: str = "data"
    log_level: str = "INFO"
    heartbeat_url: str | None = None
    starting_equity: Decimal = Decimal("10000")
    bots: tuple[BotConfig, ...] = ()


class PromotionConfig(_Cfg):
    min_trades_backtest: int = 30
    min_deflated_sharpe: Decimal = Decimal("0.95")
    min_paper_days: int = 14
    min_paper_trades: int = 20
    parity_pnl_tolerance_bp: Decimal = Decimal("1")
    min_live_small_days: int = 14


class HelmConfig(_Cfg):
    engine: EngineConfig = EngineConfig()
    risk: RiskConfig = RiskConfig()
    security: SecurityConfig = SecurityConfig()
    promotion: PromotionConfig = PromotionConfig()
