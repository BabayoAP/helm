# Helm runbook

How to set up, run, watch, stop and recover Helm on Alpaca (decision 0023).
Before any real money, work through [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md).

## The three ways to run a bot

| What | Command | Orders go to | Money |
|---|---|---|---|
| Backtest | `uv run helm backtest ...` | nowhere (simulated) | none |
| Helm paper | `uv run helm paper --feed alpaca` | nowhere: SimBroker fills on the live Alpaca feed | none |
| Alpaca paper account ("testnet") | `uv run helm live` with `environment = "paper"` | Alpaca's paper account | fake |
| Real money | `uv run helm live` with `environment = "live"` | Alpaca live | **real** |

`helm live` refuses to start unless **all** EXE-1 gates are open: `HELM_LIVE_TRADING=1`, bot `mode = "live"`, and lifecycle ≥ `LIVE_SMALL`. Real money also needs `environment = "live"` and `security.attest_no_withdrawal = true`. The base URL comes only from `environment`, so a paper config cannot reach the real-money endpoint.

## 1. One-time setup

1. `uv sync` (Python 3.12) and, for the UI, `make ui` (Node 24+).
2. Create an Alpaca account. In the dashboard, switch to **Paper Trading** and generate paper API keys. Alpaca's trading-API keys cannot withdraw or transfer money (SEC-1), but never share them anyway.
3. Copy the configs (both copies are gitignored):
   ```bash
   cp config/helm.alpaca.example.toml config/helm.toml
   cp config/risk.example.toml config/risk.toml
   ```
   For equities, raise `stale_feed_ms` in `risk.toml` to about `15000`: the free IEX feed can go quiet for seconds even in liquid names.
4. Secrets (SEC-2): never in config, never in a plaintext `.env`. Helm reads them from:
   - `--secrets env`: `HELM_SECRET_<NAME>` environment variables. Fill them from your password manager or keychain at launch, e.g. on macOS:
     ```bash
     security add-generic-password -a helm -s alpaca_paper_key_id -w   # prompts; once
     export HELM_SECRET_ALPACA_PAPER_KEY_ID=$(security find-generic-password -a helm -s alpaca_paper_key_id -w)
     ```
   - `--secrets file`: one file per secret under `/run/secrets` (Docker secrets, tmpfs).

   Names: `alpaca_paper_key_id`, `alpaca_paper_secret_key` (the `credentials` prefix in `[venues.alpaca]`), plus `telegram_bot_token` if Telegram is on.

## 2. Telegram (optional but recommended)

1. Talk to `@BotFather` → `/newbot` → keep the token as secret `telegram_bot_token`.
2. Message your bot once, then read your numeric user id and chat id from `https://api.telegram.org/bot<token>/getUpdates` (in a private browser window; don't paste the token anywhere else).
3. In `risk.toml` `[security]`: `telegram_allowed_user_ids = [<id>]`, `telegram_allowed_chat_ids = [<id>]`. In `helm.toml` `[control]`: `telegram_enabled = true`.

The bot ignores everyone else silently and logs the attempt. Commands: `/status /positions /pnl /orders /bots /pause [bot] /resume <code> /flatten /kill`. `/kill` is immediate: flatten + HALT.

## 3. Dead-man's switch (OPS-3)

Create a check at a healthchecks-style service (e.g. healthchecks.io, period 1 min, grace 3 min), and set `engine.heartbeat_url` to its ping URL. Helm pings every 60 s while healthy. If the process dies, hangs, or a flatten fails, the pings stop and the service alerts you out of band (email/SMS). With no URL set, the heartbeat is simply off.

## 4. Running

```bash
# Helm paper on the live Alpaca feed (safe default)
uv run helm paper --config config/helm.toml --risk config/risk.toml --feed alpaca --secrets env

# Alpaca paper account ("testnet"): needs a live-mode bot at LIVE_SMALL in helm.toml
HELM_LIVE_TRADING=1 uv run helm live --config config/helm.toml --risk config/risk.toml --secrets env
```

On start the engine prints `control UI: http://127.0.0.1:8765/#token=...`. Open that URL. The token stays in that browser tab only. To reach it from another device, use Tailscale (SEC-3); never expose the port publicly.

### Docker (OPS-5)

```bash
age-keygen -o ~/.config/helm/age.key                # once; keep the private key OFF the server if you can
age -r <age1 public key> -o secrets/helm.env.age <<'EOF'
HELM_SECRET_ALPACA_PAPER_KEY_ID=...
HELM_SECRET_ALPACA_PAPER_SECRET_KEY=...
EOF
BACKUP_AGE_RECIPIENT=<age1 public key> scripts/up.sh
docker compose logs -f engine                       # login URL + resume codes appear here
```

`scripts/up.sh` decrypts the secrets into its own environment only. Compose mounts them as `/run/secrets/*` (tmpfs) in the engine container. The engine runs non-root with a read-only root filesystem. Port 8765 is published on `127.0.0.1` only. `restart: unless-stopped` brings it back after crashes; recovery then runs (section 6).

The default compose command is **Helm paper** mode. Switching to `helm live` is a deliberate edit, see [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md).

## 5. Stopping things

| Need | UI | Telegram | Effect |
|---|---|---|---|
| Stop new entries, keep managing exits | Bots → Pause | `/pause` | engine/bot paused |
| Cancel a bot's resting orders | Bots → Stop | | positions kept |
| Get flat | Flatten | `/flatten` | cancel all + reduce-only close all, verified via the venue |
| Emergency | **KILL** (confirm) | `/kill` (immediate) | flatten + HALT (latched) |

After a HALT, the one-time resume code goes to the allowlisted Telegram chats and to the engine console (stderr / `docker compose logs`). It never goes to the structured log or the UI. Resume with `/resume <code>` or by entering the code in the UI banner. A halted engine stays halted across restarts (decision 0014).

If the engine itself is unreachable, use the Alpaca dashboard: **cancel all orders**, then **close all positions**.

## 6. Restarts and recovery (EXE-3)

On every start Helm rebuilds orders, fills and positions from the journal. It then asks Alpaca what happened while it was down: missed fills are fetched and applied once, and stale pending orders are marked rejected (never resubmitted). If anything is still unexplained, such as a position you opened by hand, it **refuses to trade**. Then choose:

- `--adopt`: take Alpaca's positions as they are (journaled), or
- `--flatten-first`: cancel and close everything, then start.

While running, the user stream reconnects automatically, and every reconnect triggers a resync plus a reconciliation. A mismatch pauses the engine and raises a CRITICAL alert.

## 7. Backups (OPS-6)

The `backup` compose service runs `helm backup` nightly. It snapshots every journal (safe while the engine is writing), adds the JSONL mirrors and state files, and encrypts everything to `BACKUP_AGE_RECIPIENT`. It keeps the newest 14. Parquet data is re-downloadable, so it is not included.

Restore drill (do this once, and after any storage change):

```bash
uv run helm restore backups/helm-YYYYMMDD-HHMMSS.tar.gz.age --identity ~/.config/helm/age.key --to /tmp/helm-restore
```

It refuses a non-empty target, and it verifies each journal's integrity and its append-only triggers. To actually use a restore, stop the engine, move `data/accounts/<acct>` aside, copy the restored folder in, and start with recovery as usual.

## 8. Things that are different about US equities

- **Market hours:** equity orders outside regular hours are refused (the venue clock also knows holidays). Feeds that are quiet while the market is closed don't raise stale-feed alarms.
- **Pattern day trader (SEC-9):** in a margin account under $25k, a 4th day trade within 5 business days can restrict the account. Helm warns at startup when you're close. Use a cash account or trade less often.
- **Data:** the free IEX feed is one exchange (a small slice of volume). It's fine for learning and paper trading. Consider the paid SIP feed only once a strategy looks promising.
- **Fees:** Alpaca equities are commission-free, but small regulatory fees on sells are not reported per fill. Keep fee/slippage assumptions in backtests.

## 9. Health and logs

- `GET /health` (no auth) is what the Docker healthcheck uses.
- The UI **Logs & alerts** tab shows the redacted log tail, all alerts, and every risk decision.
- The journal at `data/accounts/<acct>/journal.sqlite` is append-only. `journal.jsonl` mirrors it for reading by eye.
