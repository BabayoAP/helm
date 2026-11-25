"""RES-5: natural-language idea -> draft strategy source, via Claude.

Only the idea text and the public Strategy API description are sent. No account,
position, order, journal or credential data is ever part of the prompt: the
function signature takes a plain string and nothing else, on purpose.
The returned code is untrusted: it goes through the static allowlist and the
sandbox (`check_draft`) like any other draft, and can never promote or deploy.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

MODEL = "claude-opus-5-5"

SYSTEM = """You write trading strategy drafts for Helm, a Python 3.12 trading engine.
Return exactly one ```python code block and nothing else.

Rules the code must follow (it is rejected automatically otherwise):
- Allowed imports only: __future__, collections, collections.abc, dataclasses, decimal,
  enum, itertools, math, statistics, typing, helm.core.enums, helm.core.models,
  helm.strategy.api, helm.strategy.params, helm.strategy.examples.common.
- No I/O, network, files, environment, clock, randomness, eval/exec/open/getattr,
  dunder attributes (except super().__init__), global/nonlocal, or async code.
- Exactly one class deriving from helm.strategy.api.BaseStrategy, with a string `id`.
- Use Decimal for prices and quantities. Read history only through ctx.history(symbol,
  timeframe, n), which returns closed bars up to now; never look ahead.
- Return a list of OrderIntent from on_bar (and optionally on_quote / on_fill).
- Declare tunable parameters as `param_space = ParamSpace(dims={...})` using IntRange /
  FloatRange / Choice from helm.strategy.params, and read them with ctx.params.get(...).

API summary:
  class BaseStrategy: id: str; on_start(ctx); on_bar(bar, ctx) -> list[OrderIntent];
      on_quote(q, ctx); on_fill(f, ctx); on_stop(ctx)
  ctx.params, ctx.history(symbol, timeframe, n) -> Sequence[Bar], ctx.position(symbol).qty,
      ctx.equity(), ctx.now()
  Bar(symbol, timeframe, ts_open, ts_close, o, h, l, c, volume)
  OrderIntent(strategy_id, symbol, side=Side.BUY|SELL, type=OrderType.MARKET|LIMIT,
      qty=Decimal, limit_price=Decimal|None, reduce_only=bool, tag=str)
  helm.strategy.examples.common: sma(values), rsi(closes), market(strategy_id, bar, side, qty,
      reduce_only=False, tag="")
"""

_BLOCK = re.compile(r"```python\n(.*?)```", re.S)


class GenerationError(RuntimeError):
    pass


def build_messages(idea: str) -> list[dict[str, Any]]:
    """The only user content sent: the idea text."""
    return [{"role": "user", "content": f"Strategy idea:\n{idea.strip()}"}]


def extract_code(text: str) -> str:
    m = _BLOCK.search(text)
    if m is None:
        raise GenerationError("model did not return a ```python block")
    return m.group(1).strip() + "\n"


def generate_draft(idea: str, *, create: Callable[..., Any] | None = None) -> str:
    """Ask Claude for a draft. `create` is injectable for tests; by default the
    Anthropic SDK client (credentials from the environment / `ant auth login`)."""
    if not idea.strip():
        raise GenerationError("empty idea")
    if create is None:
        import anthropic  # noqa: PLC0415 - optional at runtime, only for this command

        create = anthropic.Anthropic().beta.messages.create
    resp = create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=build_messages(idea),
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # a safety refusal re-runs on a fallback model server-side
    )
    if getattr(resp, "stop_reason", None) == "refusal":
        raise GenerationError("the model declined this idea")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return extract_code(text)
