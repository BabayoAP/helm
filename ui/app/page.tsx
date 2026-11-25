"use client";

// Helm control panel (UI-1 header, UI-2 dashboard, UI-6 logs & alerts).
// Everything arrives over /ws (<= 10 Hz, only on change) and /api/logs.
// Controls go through /api/control, which the engine journals (SEC-7).

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type Action,
  type LogRecord,
  type Snapshot,
  control,
  dropToken,
  fetchLogs,
  fmt,
  num,
  snapshotSocket,
  takeToken,
} from "@/lib/api";
import { ChartPanel, ResearchPanel, WatchlistPanel } from "./views";

type Tab = "dashboard" | "chart" | "watchlist" | "research" | "logs";
const TABS: [Tab, string][] = [
  ["dashboard", "Dashboard"],
  ["chart", "Chart"],
  ["watchlist", "Watchlist"],
  ["research", "Research"],
  ["logs", "Logs & alerts"],
];

export default function Page() {
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [tab, setTab] = useState<Tab>("dashboard");
  const [toast, setToast] = useState<string>("");

  useEffect(() => {
    setToken(takeToken());
    setReady(true);
  }, []);

  useEffect(() => {
    if (!token) return;
    return snapshotSocket(token, setSnap, setConnected);
  }, [token]);

  const act = useCallback(
    async (action: Action, opts: { target?: string; code?: string } = {}) => {
      if (!token) return;
      try {
        const res = await control(token, action, opts);
        setToast(`${action}${opts.target ? ` ${opts.target}` : ""}: ${res}`);
      } catch (e) {
        setToast(String(e));
      }
    },
    [token],
  );

  if (!ready) return null;
  if (!token) return <Login />;
  if (!snap)
    return <div className="login card">{connected ? "Waiting for engine…" : "Connecting…"}</div>;

  return (
    <>
      <Header snap={snap} connected={connected} onAct={act} />
      <div className="tabs">
        {TABS.map(([t, label]) => (
          <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
            {label}
          </button>
        ))}
        <span className="spacer" />
        {toast && <span style={{ color: "var(--muted)", alignSelf: "center" }}>{toast}</span>}
      </div>
      <main>
        {snap.state === "HALTED" && <Halted onAct={act} />}
        {snap.reconcile_ok === false && (
          <div className="banner">Reconciliation mismatch: engine paused until resolved.</div>
        )}
        {tab === "dashboard" && <Dashboard snap={snap} onAct={act} />}
        {tab === "chart" && <ChartPanel token={token} snap={snap} />}
        {tab === "watchlist" && <WatchlistPanel rows={snap.watchlist ?? []} />}
        {tab === "research" && <ResearchPanel token={token} />}
        {tab === "logs" && <Logs token={token} snap={snap} />}
      </main>
    </>
  );
}

function Login() {
  return (
    <div className="login card">
      <h2>Helm</h2>
      <p>No session token. Open the login URL the engine printed at startup:</p>
      <p>
        <code>control UI: http://127.0.0.1:8765/#token=…</code>
      </p>
      <p style={{ color: "var(--muted)" }}>The token is kept in this tab only (sessionStorage).</p>
    </div>
  );
}

// ------------------------------------------------------------------ header (UI-1)
function Header({
  snap,
  connected,
  onAct,
}: {
  snap: Snapshot;
  connected: boolean;
  onAct: (a: Action) => Promise<void>;
}) {
  const [confirm, setConfirm] = useState(false);
  const pnl = num(snap.daily_pnl);
  const feeds = Object.entries(snap.feeds);
  const feedOk = feeds.length > 0 && feeds.every(([, s]) => s === "LIVE");
  return (
    <header className="header">
      <span className={`badge ${snap.mode}`}>{snap.mode.toUpperCase()}</span>
      <span style={{ color: "var(--muted)" }}>
        {snap.venue} / {snap.account_id}
      </span>
      <span className={`state ${snap.state}`}>{snap.state}</span>
      <div className="stat">
        <span className="k">Equity</span>
        <span className="v">{fmt(snap.equity)}</span>
      </div>
      <div className="stat">
        <span className="k">Day PnL</span>
        <span className={`v ${pnl >= 0 ? "up" : "down"}`}>
          {pnl >= 0 ? "+" : ""}
          {fmt(snap.daily_pnl)}
        </span>
      </div>
      <div className="stat">
        <span className="k">Feed</span>
        <span className={`v ${feedOk ? "up" : "down"}`}>
          {feeds.length === 0 ? "–" : feedOk ? "LIVE" : feeds.filter(([, s]) => s !== "LIVE").map(([k, s]) => `${k} ${s}`).join(", ")}
        </span>
      </div>
      <div className="stat">
        <span className="k">Link</span>
        <span className={`v ${connected ? "up" : "down"}`}>{connected ? "ok" : "lost"}</span>
      </div>
      <span className="spacer" />
      <button onClick={() => void onAct("flatten")}>Flatten</button>
      <button className="kill" onClick={() => setConfirm(true)} disabled={snap.state === "HALTED"}>
        KILL
      </button>
      {confirm && (
        <div className="popover" role="dialog">
          <p>
            <b>Flatten everything and HALT?</b>
            <br />
            Cancels all orders, closes all positions, and latches HALTED. Resuming needs the
            one-time code.
          </p>
          <div className="row">
            <button
              className="kill"
              onClick={() => {
                setConfirm(false);
                void onAct("kill");
              }}
            >
              Confirm kill
            </button>
            <button onClick={() => setConfirm(false)}>Cancel</button>
          </div>
        </div>
      )}
    </header>
  );
}

function Halted({ onAct }: { onAct: (a: Action, o?: { code?: string }) => Promise<void> }) {
  const [code, setCode] = useState("");
  return (
    <div className="banner row">
      <b>HALTED.</b>
      <span>Enter the resume code sent to Telegram / the engine console:</span>
      <input value={code} onChange={(e) => setCode(e.target.value.trim())} placeholder="code" />
      <button disabled={!code} onClick={() => void onAct("resume", { code })}>
        Resume
      </button>
    </div>
  );
}

// --------------------------------------------------------------- dashboard (UI-2)
function Dashboard({
  snap,
  onAct,
}: {
  snap: Snapshot;
  onAct: (a: Action, o?: { target?: string }) => Promise<void>;
}) {
  return (
    <>
      <section className="card w8">
        <h2>Equity</h2>
        <EquityCurve points={snap.equity_curve} />
      </section>
      <section className="card w4">
        <h2>Risk utilization</h2>
        <div className="risk">
          {snap.risk.map((r) => {
            const pct = Math.min(100, (num(r.used) / Math.max(num(r.limit), 1e-12)) * 100);
            const color = pct >= 80 ? "var(--down)" : pct >= 50 ? "var(--warn)" : "var(--up)";
            return (
              <div key={r.name}>
                <div className="lbl">
                  <span>{r.name}</span>
                  <span style={{ fontFamily: "var(--mono)" }}>
                    {fmt(r.used)} / {fmt(r.limit)}
                  </span>
                </div>
                <div className="bar">
                  <div style={{ width: `${pct}%`, background: color }} />
                </div>
              </div>
            );
          })}
        </div>
      </section>
      <section className="card w6">
        <h2>Positions</h2>
        {snap.positions.length === 0 ? (
          <div className="empty">Flat</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="n">Qty</th>
                <th className="n">Avg</th>
                <th className="n">Mark</th>
                <th className="n">uPnL</th>
                <th className="n">rPnL</th>
              </tr>
            </thead>
            <tbody>
              {snap.positions.map((p) => (
                <tr key={p.symbol}>
                  <td>{p.symbol}</td>
                  <td className="n">{p.qty}</td>
                  <td className="n">{fmt(p.avg_px, 4)}</td>
                  <td className="n">{fmt(p.mark, 4)}</td>
                  <td className={`n ${num(p.unrealized_pnl) >= 0 ? "up" : "down"}`}>{fmt(p.unrealized_pnl)}</td>
                  <td className={`n ${num(p.realized_pnl) >= 0 ? "up" : "down"}`}>{fmt(p.realized_pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="card w6">
        <h2>Open orders</h2>
        {snap.orders.length === 0 ? (
          <div className="empty">None</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Id</th>
                <th>Symbol</th>
                <th>Side</th>
                <th>Type</th>
                <th className="n">Qty</th>
                <th className="n">Filled</th>
                <th className="n">Limit</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {snap.orders.map((o) => (
                <tr key={o.client_order_id}>
                  <td>{o.client_order_id}</td>
                  <td>{o.symbol}</td>
                  <td className={o.side === "buy" ? "up" : "down"}>{o.side}</td>
                  <td>{o.type}</td>
                  <td className="n">{o.qty}</td>
                  <td className="n">{o.filled_qty}</td>
                  <td className="n">{o.limit_price ?? "–"}</td>
                  <td>{o.state}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="card w12">
        <h2>Bots</h2>
        {snap.bots.length === 0 ? (
          <div className="empty">No bots loaded</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Bot</th>
                <th>Symbols</th>
                <th>Lifecycle</th>
                <th>Status</th>
                <th className="n">Rejects</th>
                <th>Controls</th>
              </tr>
            </thead>
            <tbody>
              {snap.bots.map((b) => (
                <tr key={b.id}>
                  <td>{b.id}</td>
                  <td>{b.symbols.join(", ")}</td>
                  <td>{b.lifecycle}</td>
                  <td className={b.paused ? "down" : "up"}>{b.paused ? "paused" : "active"}</td>
                  <td className="n">{b.consecutive_rejects}</td>
                  <td>
                    <div className="row">
                      <button onClick={() => void onAct("start", { target: b.id })}>Start</button>
                      <button onClick={() => void onAct("pause", { target: b.id })}>Pause</button>
                      <button onClick={() => void onAct("stop", { target: b.id })}>Stop</button>
                      <button onClick={() => void onAct("flatten")}>Flatten</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

function EquityCurve({ points }: { points: [number, string][] }) {
  const path = useMemo(() => {
    if (points.length < 2) return null;
    const ys = points.map(([, v]) => num(v));
    const lo = Math.min(...ys);
    const hi = Math.max(...ys);
    const span = hi - lo || 1;
    const W = 800;
    const H = 200;
    const d = ys
      .map((y, i) => `${i === 0 ? "M" : "L"}${((i / (ys.length - 1)) * W).toFixed(1)},${(H - ((y - lo) / span) * (H - 10) - 5).toFixed(1)}`)
      .join(" ");
    return { d, lo, hi, up: ys[ys.length - 1] >= ys[0] };
  }, [points]);
  if (!path) return <div className="empty">Equity curve appears after the first closed bars.</div>;
  return (
    <div>
      <svg viewBox="0 0 800 200" width="100%" height="200" preserveAspectRatio="none" aria-label="equity curve">
        <path d={path.d} fill="none" stroke={path.up ? "var(--up)" : "var(--down)"} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="row" style={{ justifyContent: "space-between", color: "var(--muted)", fontFamily: "var(--mono)" }}>
        <span>low {path.lo.toFixed(2)}</span>
        <span>high {path.hi.toFixed(2)}</span>
      </div>
    </div>
  );
}

// ------------------------------------------------------------ logs & alerts (UI-6)
function Logs({ token, snap }: { token: string; snap: Snapshot }) {
  const [logs, setLogs] = useState<LogRecord[]>([]);
  const [filter, setFilter] = useState("");
  const [level, setLevel] = useState("all");
  const [err, setErr] = useState("");

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const rows = await fetchLogs(token);
        if (live) {
          setLogs(rows);
          setErr("");
        }
      } catch (e) {
        if (String(e).includes("unauthorized")) dropToken();
        if (live) setErr(String(e));
      }
    };
    void load();
    const t = setInterval(load, 2000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [token]);

  const shown = logs
    .filter((r) => level === "all" || r.level === level)
    .filter((r) => !filter || JSON.stringify(r).toLowerCase().includes(filter.toLowerCase()))
    .slice()
    .reverse();

  return (
    <>
      <section className="card w6">
        <h2>Alerts</h2>
        <div className="scroll">
          {snap.alerts.length === 0 ? (
            <div className="empty">No alerts</div>
          ) : (
            <table>
              <tbody>
                {snap.alerts
                  .slice()
                  .reverse()
                  .map((a, i) => (
                    <tr key={`${a.ts}-${i}`}>
                      <td>{new Date(a.ts / 1e6).toLocaleTimeString()}</td>
                      <td className={`sev-${a.severity}`}>{a.severity}</td>
                      <td style={{ whiteSpace: "normal" }}>{a.message}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
      <section className="card w6">
        <h2>Risk decisions (audit)</h2>
        <div className="scroll">
          {snap.decisions.length === 0 ? (
            <div className="empty">No decisions yet</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Intent</th>
                  <th>Verdict</th>
                  <th>Rule</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {snap.decisions
                  .slice()
                  .reverse()
                  .map((d, i) => (
                    <tr key={`${d.intent_id}-${i}`}>
                      <td>{d.intent_id}</td>
                      <td className={d.verdict === "REJECT" ? "down" : "up"}>{d.verdict}</td>
                      <td>{d.rule_id}</td>
                      <td style={{ whiteSpace: "normal" }}>{d.reason}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
      <section className="card w12">
        <h2>Log tail</h2>
        <div className="row" style={{ marginBottom: 8 }}>
          <input placeholder="filter text" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            {["all", "debug", "info", "warning", "error", "critical"].map((l) => (
              <option key={l}>{l}</option>
            ))}
          </select>
          {err && <span className="down">{err}</span>}
        </div>
        <div className="scroll">
          <table>
            <tbody>
              {shown.map((r, i) => {
                const { timestamp, level: lv, event, ...rest } = r;
                return (
                  <tr key={i}>
                    <td>{String(timestamp ?? "")}</td>
                    <td className={`lvl-${String(lv)}`}>{String(lv ?? "")}</td>
                    <td>{String(event ?? "")}</td>
                    <td style={{ whiteSpace: "normal", color: "var(--muted)" }}>
                      {Object.entries(rest)
                        .map(([k, v]) => `${k}=${String(v)}`)
                        .join(" ")}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
