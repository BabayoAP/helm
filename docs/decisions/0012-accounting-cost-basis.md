# 0012 — Accounting: cost basis, margin-style cash, single quote currency
- **Context:** BT-8 requires `equity_T = equity_0 + Σrealized + unrealized − Σfees` to hold for random fill sequences; average-price division in Decimal broke exact equality (~1e-23).
- **Decision:** positions track signed *cost basis*; partial-close cost is quantized to 1e-12 and moves between realized and unrealized, cancelling in equity. Cash = equity_0 + realized − fees − funding. One quote currency per portfolio (multi-currency is Phase 2).
- **Consequence:** the identity holds exactly, cross-checked against an independent cash-flow computation (Hypothesis). Lives in `helm.oms.accounting` so backtest, paper and live share it.
