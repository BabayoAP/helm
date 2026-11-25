# 0027 — UI: minimal Next.js, static export, served same-origin by the control API
- **Context:** UI-1/2/6 are the M4 scope. UI-8 requires localhost/Tailscale only, a local session token, CSRF-safe controls, and no secrets sent to the browser.
- **Decision:**
  - `ui/` is a Next.js app built with `output: "export"`. FastAPI serves `ui/out` at `/`, so there is no CORS and no second server.
  - The engine prints a login URL carrying a random session token. The UI keeps the token in `sessionStorage` and sends it as `Authorization: Bearer`.
  - Every control POST also needs the `X-Helm-CSRF: 1` header and a same-origin `Origin`.
  - The API binds `127.0.0.1` by default.
- **Consequence:** the UI talks only to `/api/*` and `/ws`. It never sees venue keys.
