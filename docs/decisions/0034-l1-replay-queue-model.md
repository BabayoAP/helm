# 0034 — BT-4: L1 replay with a queue-position model
- **Context:** BT-4 asks for tick/L1/L2 replay with a queue estimate for passive orders. Alpaca's feed is L1 only (quotes + trades), so the replay is L1.
- **Decision:**
  - `helm.app.backtest.run_replay` (`helm backtest --replay <session>`) runs a recorded session (`helm paper --record`) through the same engine, with SimBroker in L1 mode and the clock following each event's arrival time. It is deterministic (I7).
  - With `queue_model=True`, the default for replays, a resting limit order joins the queue as follows:
    - at the touch: behind the displayed size;
    - improving the touch: first in line;
    - behind the touch: unknown until its level becomes the touch.
  - The queue ahead shrinks when trades print at the order's price, and when displayed size at the level drops (assumed to be cancellations ahead of us). Prints at the limit fill the order only once the queue ahead is gone. Prints through the limit always fill.
- **Consequence:** paper mode keeps the conservative through-only model (unchanged, so TEST-4 parity is untouched). Replays can quantify how much passive fills depend on queue position: compare `--no-queue`.
