// Thin client for the Helm control API (decision 0027, UI-8).
// The session token comes from the login URL fragment (#token=...), lives only in
// sessionStorage, and is sent as a Bearer token. Control POSTs add the CSRF header.

export type Mode = "backtest" | "paper" | "live";
export type EngineState = "RUNNING" | "PAUSED" | "HALTED";

export interface PositionView {
  symbol: string;
  qty: string;
  avg_px: string;
  mark: string | null;
  unrealized_pnl: string;
  realized_pnl: string;
}
export interface OrderView {
  client_order_id: string;
  strategy_id: string;
  symbol: string;
  side: string;
  type: string;
  qty: string;
  filled_qty: string;
  limit_price: string | null;
  state: string;
}
export interface BotView {
  id: string;
  symbols: string[];
  lifecycle: string;
  paused: boolean;
  consecutive_rejects: number;
}
export interface RiskUtilization {
  name: string;
  used: string;
  limit: string;
}
export interface AlertView {
  severity: "INFO" | "WARN" | "CRITICAL";
  key: string;
  message: string;
  mode: Mode;
  ts: number;
}
export interface DecisionView {
  intent_id: string;
  verdict: string;
  rule_id: string;
  reason: string;
}
export interface Snapshot {
  mode: Mode;
  venue: string;
  account_id: string;
  state: EngineState;
  equity: string;
  daily_pnl: string;
  feeds: Record<string, string>;
  positions: PositionView[];
  orders: OrderView[];
  bots: BotView[];
  risk: RiskUtilization[];
  equity_curve: [number, string][];
  alerts: AlertView[];
  decisions: DecisionView[];
  reconcile_ok: boolean | null;
}
export type LogRecord = Record<string, string | number | boolean | null>;
export type Action = "start" | "stop" | "pause" | "resume" | "flatten" | "kill";

const KEY = "helm.token";

export function takeToken(): string | null {
  if (typeof window === "undefined") return null;
  const m = window.location.hash.match(/token=([A-Za-z0-9_-]+)/);
  if (m) {
    sessionStorage.setItem(KEY, m[1]);
    history.replaceState(null, "", window.location.pathname); // strip it from the URL bar
  }
  return sessionStorage.getItem(KEY);
}

export function dropToken(): void {
  sessionStorage.removeItem(KEY);
}

export async function fetchLogs(token: string, n = 300): Promise<LogRecord[]> {
  const r = await fetch(`/api/logs?n=${n}`, { headers: { Authorization: `Bearer ${token}` } });
  if (r.status === 401) throw new Error("unauthorized");
  if (!r.ok) throw new Error(`logs: HTTP ${r.status}`);
  return (await r.json()) as LogRecord[];
}

export async function control(
  token: string,
  action: Action,
  opts: { target?: string; code?: string } = {},
): Promise<string> {
  const r = await fetch("/api/control", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "X-Helm-CSRF": "1",
    },
    body: JSON.stringify({ action, target: opts.target ?? null, code: opts.code ?? null }),
  });
  if (!r.ok) throw new Error(`${action}: HTTP ${r.status}`);
  return ((await r.json()) as { result: string }).result;
}

export function snapshotSocket(
  token: string,
  onSnap: (s: Snapshot) => void,
  onState: (connected: boolean) => void,
): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry = 500;
  const open = () => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${window.location.host}/ws?token=${encodeURIComponent(token)}`);
    ws.onopen = () => {
      retry = 500;
      onState(true);
    };
    ws.onmessage = (ev) => onSnap(JSON.parse(ev.data as string) as Snapshot);
    ws.onclose = () => {
      onState(false);
      if (!closed) setTimeout(open, (retry = Math.min(retry * 2, 10_000)));
    };
  };
  open();
  return () => {
    closed = true;
    ws?.close();
  };
}

export const num = (s: string | null | undefined): number => (s == null ? NaN : Number(s));
export const fmt = (s: string | null | undefined, digits = 2): string => {
  const n = num(s);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits }) : "–";
};
