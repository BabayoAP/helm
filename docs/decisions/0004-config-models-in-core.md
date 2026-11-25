# 0004 — Config models in `helm.core.config`, loaders in `helm.infra.config`
- **Context:** `risk.*` may import only `core` (§7) but needs `RiskConfig`.
- **Decision:** Pydantic config models (frozen) live in core; TOML reading, secret-key rejection and file I/O live in infra.
- **Consequence:** risk stays pure. The loader refuses any key that looks like a secret (SEC-2), so secrets can only come from the secrets store.
