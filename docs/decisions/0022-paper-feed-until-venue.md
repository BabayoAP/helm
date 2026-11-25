# 0022 — Paper mode runs on a synthetic live feed until the M4 venue is chosen
- **Context:** PAP-1 needs a live feed; the first venue is a BLOCKING owner decision at M4.
- **Decision:** `helm paper` uses `synthetic_feed` (seeded random walk of quotes + trades, bars built from trades) with the real engine, risk, OMS, SimBroker (L1 live-fill model), journal, account lock and recovery. The venue feed handler replaces it in M4 without engine changes.
- **Consequence:** the 24 h soak and DATA-4 24 h recording exit criteria need the real feed and remain open.
