# 0015 — Flatten / exit exemptions inside the risk gate
- **Context:** §10: flatten bypasses RISK-3/5/6/7 but never reduce-only. Literal RISK-4 (order notional cap) and RISK-2 (needs a reference price) could block a flatten of a large position or during a feed outage, exactly when it matters.
- **Decision:** (1) flatten intents must be reduce-only and reduce the position (else REJECT). (2) If no price exists at all, a reduce-only flatten is still allowed. (3) The order-notional cap and `min_notional` apply only to orders that do not purely close existing exposure. (4) RISK-5/6/8/9 reject only orders that *increase* exposure, so de-risking is never blocked.
- **Consequence:** the Hypothesis property test asserts no approved exposure-increasing order breaches RISK-4/5/6.
