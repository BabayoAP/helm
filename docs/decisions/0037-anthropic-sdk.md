# 0037 — Anthropic SDK for idea → draft (RES-5)
- **Decision:** add `anthropic` (official SDK). It is used only by `helm research draft --idea`. Model `claude-opus-5-5` with effort `high`, plus server-side refusal fallback (`server-side-fallback-2026-07-01`, `fallbacks: "default"`). A refusal is reported, never retried silently.
- **Privacy:** `generate_draft(idea)` takes only the idea string. The prompt is that text plus a fixed description of the public Strategy API. Account, position, order and journal data and secrets are never sent (tested).
- **Credentials:** the SDK's normal resolution (`ANTHROPIC_API_KEY` or `ant auth login`), in the parent CLI process only. The sandbox child never has them.
- **Import rule:** strategies are forbidden from importing `anthropic`, like every other network library.
