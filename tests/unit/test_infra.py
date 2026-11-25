"""Logging redaction (OPS-4, TEST-8), config loading, secrets store (SEC-2)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from helm.core.config import RiskConfig
from helm.core.errors import ConfigError
from helm.infra.config import load_config, load_toml
from helm.infra.logging import REDACTED, SECRETS, configure_logging, get_logger
from helm.infra.secrets import EnvSecretStore, FileSecretStore

ROOT = Path(__file__).resolve().parents[2]
FAKE_SECRET = "sk_test_NOT_A_REAL_KEY_1234567890"  # gitleaks:allow


def _log_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(x) for x in stream.getvalue().splitlines()]


def test_known_secret_never_appears_in_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    SECRETS.clear()
    monkeypatch.setenv("HELM_SECRET_EXCHANGE_KEY", FAKE_SECRET)
    assert EnvSecretStore().get("exchange_key") == FAKE_SECRET
    out = io.StringIO()
    configure_logging(stream=out, mode_badge="[PAPER]")
    log = get_logger(run_id="r1")
    log.info("connecting", url=f"wss://x?key={FAKE_SECRET}", nested={"a": [FAKE_SECRET]})
    log.info("auth", api_key="whatever", password="pw", token=None)
    try:
        raise RuntimeError(f"bad key {FAKE_SECRET}")
    except RuntimeError:
        log.exception("boom")
    text = out.getvalue()
    assert FAKE_SECRET not in text
    lines = _log_lines(out)
    assert lines[0]["mode"] == "[PAPER]" and lines[0]["run_id"] == "r1"
    assert lines[1]["api_key"] == REDACTED and lines[1]["password"] == REDACTED
    assert lines[1]["token"] is None
    SECRETS.clear()


def test_secret_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HELM_SECRET_MISSING", raising=False)
    with pytest.raises(ConfigError):
        EnvSecretStore().get("missing")
    (tmp_path / "tg_token").write_text("abcd-efgh\n")
    assert FileSecretStore(tmp_path).get("tg_token") == "abcd-efgh"
    with pytest.raises(ConfigError):
        FileSecretStore(tmp_path).get("nope")
    with pytest.raises(ConfigError):
        FileSecretStore(tmp_path).get(".env")
    SECRETS.clear()


def test_example_configs_load() -> None:
    cfg = load_config(
        ROOT / "config/helm.example.toml",
        ROOT / "config/risk.example.toml",
        ROOT / "config/promotion.example.toml",
    )
    assert cfg.risk == RiskConfig()  # example mirrors PRD defaults (Appendix A)
    assert cfg.engine.bots[0].id == "sma-btc"
    assert cfg.promotion.min_paper_days == 14


def test_config_rejects_secrets_and_bad_values(tmp_path: Path) -> None:
    p = tmp_path / "h.toml"
    p.write_text('[engine]\nexchange_api_key = "x"\n')
    with pytest.raises(ConfigError, match="secret"):
        load_toml(p)
    p.write_text("[risk]\nmax_gross_leverage = -1\n")
    with pytest.raises(ConfigError):
        load_config(risk_path=p)
    with pytest.raises(ConfigError):
        load_toml(tmp_path / "missing.toml")


def test_risk_config_loosen_detection() -> None:
    base = RiskConfig(symbol_allowlist=("a:B/C",))
    tighter = base.model_copy(update={"max_gross_leverage": 1})
    assert base.tightening_violations(tighter) == []
    looser = base.model_copy(
        update={
            "max_gross_leverage": 3,
            "symbol_allowlist": (),
            "on_daily_loss": "cancel_and_halt",
            "feed_healthy_clear_s": 0,
        }
    )
    assert base.tightening_violations(looser) == [
        "feed_healthy_clear_s",
        "max_gross_leverage",
        "on_daily_loss",
        "symbol_allowlist",
    ]
    assert base.tightening_violations(base.model_copy(update={"symbol_allowlist": ("x:Y/Z",)})) == [
        "symbol_allowlist"
    ]
