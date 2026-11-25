# 0026 — Control plane talks to the engine through `helm.core.control.ControlPort`
- **Context:** an import contract says `helm.api` must never import brokers or secrets. Importing `helm.app.engine` would reach brokers indirectly.
- **Decision:** `helm.core.control` defines the `ControlPort` protocol (`snapshot()`, `command()`) and the `EngineSnapshot` model. `helm.app.control.EnginePort` implements it around an `Engine`. `helm.api` (FastAPI) and `helm.telegram` (bot) depend only on the port. Every command still goes through `Engine.control`, which journals who/when/from where (SEC-7).
- **Consequence:** the API and the bot can be tested against a fake port, with no engine involved.
