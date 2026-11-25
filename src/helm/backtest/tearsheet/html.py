"""Self-contained HTML tearsheet (BT-6): metrics table + equity and drawdown curves as inline SVG.

The header always states which engine produced the result (vectorized =
screening, event-driven = evidence); only event-driven results count toward
promotion (PRD §9.4).
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PERCENT = {"total_return", "cagr", "max_drawdown", "win_rate", "exposure", "psr", "deflated_sharpe"}


def _fmt(k: str, v: Any) -> str:
    if isinstance(v, float):
        if k in PERCENT:
            return f"{v * 100:.2f}%"
        return f"{v:,.4f}" if abs(v) < 1000 else f"{v:,.2f}"
    if isinstance(v, dict):
        return ", ".join(
            f"{kk} {_fmt('total_return', vv) if kk != 'p_loss' else f'{vv:.0%}'}"
            for kk, vv in v.items()
        )  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]
    return html.escape(str(v))


def _polyline(points: Sequence[tuple[float, float]], w: int, h: int, pad: int = 4) -> str:
    if len(points) < 2:
        return ""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    sx = (w - 2 * pad) / ((x1 - x0) or 1)
    sy = (h - 2 * pad) / ((y1 - y0) or 1)
    step = max(1, len(points) // 1500)
    pts = " ".join(
        f"{pad + (x - x0) * sx:.1f},{h - pad - (y - y0) * sy:.1f}" for x, y in points[::step]
    )
    return pts


def render(
    out_dir: Path | str,
    *,
    title: str,
    engine: str,
    metrics: dict[str, Any],
    curve: Sequence[tuple[int, float]],
    meta: dict[str, Any],
    warnings: Sequence[str] = (),
) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    eq = [(float(t), float(v)) for t, v in curve]
    peak = -float("inf")
    dd: list[tuple[float, float]] = []
    for t, v in eq:
        peak = max(peak, v)
        dd.append((t, (v - peak) / peak if peak > 0 else 0.0))
    label = "EVENT-DRIVEN · evidence" if engine == "event_driven" else "VECTORIZED · screening only"
    rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{_fmt(k, v)}</td></tr>" for k, v in metrics.items()
    )
    meta_rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td><code>{html.escape(str(v))}</code></td></tr>"
        for k, v in meta.items()
    )
    warn = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--line:#30363d;--eq:#58a6ff;--dd:#f85149;--badge:#1f6feb}}
@media (prefers-color-scheme: light){{:root{{--bg:#fff;--fg:#1f2328;--mut:#59636e;--line:#d1d9e0;--eq:#0969da;--dd:#cf222e}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0;padding:16px;max-width:1100px}}
h1{{font-size:18px;margin:0 0 4px}} .badge{{display:inline-block;background:var(--badge);color:#fff;padding:2px 8px;border-radius:4px;font-size:12px}}
.mut{{color:var(--mut)}} table{{border-collapse:collapse;width:100%}} th,td{{text-align:left;padding:3px 8px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-weight:normal;width:40%}} svg{{width:100%;height:auto;border:1px solid var(--line);margin:8px 0}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} @media (max-width:700px){{.grid{{grid-template-columns:1fr}}}}
code{{word-break:break-all}} ul.warn li{{color:var(--dd)}}
</style></head><body>
<h1>{html.escape(title)}</h1><span class="badge">{label}</span> <span class="mut">generated {generated}</span>
<ul class="warn">{warn}</ul>
<h2>Equity</h2><svg viewBox="0 0 1000 260"><polyline fill="none" stroke="var(--eq)" stroke-width="1.5" points="{_polyline(eq, 1000, 260)}"/></svg>
<h2>Drawdown</h2><svg viewBox="0 0 1000 140"><polyline fill="none" stroke="var(--dd)" stroke-width="1.5" points="{_polyline(dd, 1000, 140)}"/></svg>
<div class="grid"><div><h2>Metrics</h2><table>{rows}</table></div><div><h2>Run</h2><table>{meta_rows}</table></div></div>
</body></html>"""
    path = out / "tearsheet.html"
    path.write_text(page, encoding="utf-8")
    (out / "result.json").write_text(
        json.dumps(
            {"engine": engine, "metrics": metrics, "meta": meta, "warnings": list(warnings)},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path
