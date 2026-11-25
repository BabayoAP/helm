# 0016 — Latency in bar mode
- **Context:** BT-5 asks for a 50–250 ms seeded latency model. In bar mode an order decided at a close is filled at the next bar's open.
- **Decision:** each order gets an arrival time = decision time + sampled latency; it only matches bars that close after arrival. For bars longer than the latency this changes nothing; the latency matters fully in L1/paper mode, where it gates which quote/trade an order can see.
- **Consequence:** deterministic (seeded) and honest; intrabar latency effects need tick replay (BT-4, Phase 2).
