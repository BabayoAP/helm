"use client";

// Phase 2 UI views: UI-3 chart (Lightweight Charts), UI-4 watchlist, UI-5 research.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  type IChartApi,
  type UTCTimestamp,
  createChart,
  createSeriesMarkers,
} from "lightweight-charts";
import { type ChartView, type ResearchView, type Snapshot, type WatchRow, fetchChart, fetchResearch, fmt, num } from "@/lib/api";

// ------------------------------------------------------------------ UI-3 chart
export function ChartPanel({ token, snap }: { token: string; snap: Snapshot }) {
  const symbols = useMemo(() => {
    const s = new Set<string>();
    snap.bots.forEach((b) => b.symbols.forEach((x) => s.add(x)));
    snap.positions.forEach((p) => s.add(p.symbol));
    return [...s];
  }, [snap.bots, snap.positions]);
  const [symbol, setSymbol] = useState<string>("");
  const [view, setView] = useState<ChartView | null>(null);
  const [err, setErr] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const sym = symbol || symbols[0] || "";

  useEffect(() => {
    if (!sym) return;
    let live = true;
    const load = async () => {
      try {
        const v = await fetchChart(token, sym);
        if (live) {
          setView(v);
          setErr(v ? "" : "no bars yet for this symbol");
        }
      } catch (e) {
        if (live) setErr(String(e));
      }
    };
    void load();
    const t = setInterval(load, 5000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [token, sym]);

  useEffect(() => {
    if (!box.current || !view) return;
    chart.current?.remove();
    const c = createChart(box.current, {
      height: 420,
      layout: { background: { color: "#161b22" }, textColor: "#8b949e" },
      grid: { vertLines: { color: "#1f242c" }, horzLines: { color: "#1f242c" } },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    chart.current = c;
    const series = c.addSeries(CandlestickSeries, {
      upColor: "#3fb950",
      downColor: "#f85149",
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
      borderVisible: false,
    });
    const t = (ns: number) => Math.floor(ns / 1e9) as UTCTimestamp;
    series.setData(view.bars.map((b) => ({ time: t(b.ts), open: num(b.o), high: num(b.h), low: num(b.l), close: num(b.c) })));
    const first = view.bars.length ? view.bars[0].ts : 0;
    createSeriesMarkers(
      series,
      view.markers
        .filter((m) => m.ts >= first)
        .map((m) => ({
          time: t(m.ts),
          position: m.side === "buy" ? ("belowBar" as const) : ("aboveBar" as const),
          shape: m.side === "buy" ? ("arrowUp" as const) : ("arrowDown" as const),
          color: m.side === "buy" ? "#3fb950" : "#f85149",
          text: `${m.side} ${m.qty}`,
        }))
        .sort((a, b) => a.time - b.time),
    );
    view.order_lines.forEach((o) =>
      series.createPriceLine({
        price: num(o.price),
        color: o.side === "buy" ? "#2f81f7" : "#d29922",
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: `${o.side} ${o.qty}`,
      }),
    );
    c.timeScale().fitContent();
    const onResize = () => box.current && c.applyOptions({ width: box.current.clientWidth });
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [view]);

  useEffect(() => () => chart.current?.remove(), []);

  return (
    <section className="card w12">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Chart</h2>
        <select value={sym} onChange={(e) => setSymbol(e.target.value)}>
          {symbols.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        {view && <span style={{ color: "var(--muted)" }}>{view.timeframe} · {view.bars.length} bars · {view.markers.length} fills · {view.order_lines.length} resting</span>}
        {err && <span className="down">{err}</span>}
      </div>
      <div ref={box} style={{ width: "100%" }} />
    </section>
  );
}

// -------------------------------------------------------------- UI-4 watchlist
type Key = "symbol" | "last" | "change_24h_pct" | "spread_pct" | "volume_z";
const COLS: [Key, string][] = [
  ["symbol", "Symbol"],
  ["last", "Last"],
  ["change_24h_pct", "24h %"],
  ["spread_pct", "Spread %"],
  ["volume_z", "Vol z"],
];

export function WatchlistPanel({ rows }: { rows: WatchRow[] }) {
  const [sort, setSort] = useState<{ key: Key; desc: boolean }>({ key: "symbol", desc: false });
  const sorted = useMemo(() => {
    const val = (r: WatchRow) => (sort.key === "symbol" ? r.symbol : num(r[sort.key] as string | null));
    return [...rows].sort((a, b) => {
      const x = val(a);
      const y = val(b);
      const nx = typeof x === "number" && !Number.isFinite(x);
      const ny = typeof y === "number" && !Number.isFinite(y);
      if (nx !== ny) return nx ? 1 : -1; // blanks last
      const c = x < y ? -1 : x > y ? 1 : 0;
      return sort.desc ? -c : c;
    });
  }, [rows, sort]);
  if (rows.length === 0) return <section className="card w12"><h2>Watchlist</h2><div className="empty">Add symbols under [tracking] watch_symbols.</div></section>;
  return (
    <section className="card w12">
      <h2>Watchlist</h2>
      <table>
        <thead>
          <tr>
            {COLS.map(([k, label]) => (
              <th
                key={k}
                className={k === "symbol" ? "" : "n"}
                style={{ cursor: "pointer" }}
                onClick={() => setSort((s) => ({ key: k, desc: s.key === k ? !s.desc : k !== "symbol" }))}
              >
                {label}
                {sort.key === k ? (sort.desc ? " ▼" : " ▲") : ""}
              </th>
            ))}
            <th>Traded</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => {
            const ch = num(r.change_24h_pct);
            const z = r.volume_z ?? NaN;
            const sp = num(r.spread_pct);
            return (
              <tr key={r.symbol}>
                <td>{r.symbol}</td>
                <td className="n">{fmt(r.last, 4)}</td>
                <td className={`n ${ch >= 0 ? "up" : "down"}`}>{Number.isFinite(ch) ? `${ch >= 0 ? "+" : ""}${ch.toFixed(2)}` : "–"}</td>
                <td className={`n ${sp > 0.5 ? "down" : ""}`}>{Number.isFinite(sp) ? sp.toFixed(3) : "–"}</td>
                <td className={`n ${Math.abs(z) >= 3 ? "anomaly" : ""}`}>{Number.isFinite(z) ? z.toFixed(1) : "–"}</td>
                <td>{r.traded ? "yes" : ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

// --------------------------------------------------------------- UI-5 research
export function ResearchPanel({ token }: { token: string }) {
  const [data, setData] = useState<ResearchView | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    fetchResearch(token).then(setData, (e: unknown) => setErr(String(e)));
  }, [token]);
  if (err) return <section className="card w12"><span className="down">{err}</span></section>;
  if (!data) return <section className="card w12">Loading…</section>;
  return (
    <>
      <section className="card w12">
        <h2>Experiments</h2>
        {data.experiments.length === 0 ? (
          <div className="empty">No runs yet: helm backtest / helm research validate record here.</div>
        ) : (
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Kind</th>
                  <th>Verdict</th>
                  <th>Strategy</th>
                  <th>Params</th>
                  <th className="n">Sharpe</th>
                  <th className="n">Trades</th>
                  <th>Code</th>
                  <th>Git</th>
                </tr>
              </thead>
              <tbody>
                {data.experiments.map((e) => (
                  <tr key={e.run_id} onClick={() => setOpen(open === e.run_id ? null : e.run_id)} style={{ cursor: e.failures.length ? "pointer" : "default" }}>
                    <td>{new Date(e.ts / 1e6).toLocaleString()}</td>
                    <td>{e.kind}</td>
                    <td className={e.passed === true ? "up" : e.passed === false ? "down" : ""}>
                      {e.passed === null ? "" : e.passed ? "PASS" : "REJECT"}
                      {open === e.run_id && e.failures.length > 0 && (
                        <div style={{ whiteSpace: "normal", color: "var(--muted)" }}>{e.failures.join(" · ")}</div>
                      )}
                    </td>
                    <td>{e.strategy.split(":").pop()}</td>
                    <td>{JSON.stringify(e.params)}</td>
                    <td className="n">{e.sharpe == null ? "–" : e.sharpe.toFixed(2)}</td>
                    <td className="n">{e.trades ?? "–"}</td>
                    <td>{e.code_hash.slice(0, 8)}</td>
                    <td>{e.git_hash.slice(0, 8)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card w12">
        <h2>Promotion log (§13)</h2>
        {data.promotions.length === 0 ? (
          <div className="empty">Nothing promoted yet. Use helm promote request --to … --approve-as you.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Strategy</th>
                <th>Version</th>
                <th>Change</th>
                <th>By</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {data.promotions
                .slice()
                .reverse()
                .map((p, i) => (
                  <tr key={i}>
                    <td>{new Date(p.ts / 1e6).toLocaleString()}</td>
                    <td>{p.strategy.split(":").pop()}</td>
                    <td>{p.code_hash.slice(0, 8)}/{p.params_hash.slice(0, 8)}</td>
                    <td>
                      {p.from_stage} → <b>{p.to_stage}</b>
                    </td>
                    <td>{p.approved_by}</td>
                    <td style={{ whiteSpace: "normal" }}>{p.note}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
