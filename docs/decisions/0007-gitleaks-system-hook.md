# 0007 — gitleaks runs as a system binary
- **Context:** §5 requires gitleaks in pre-commit and CI. The upstream pre-commit hook needs a Go toolchain.
- **Decision:** pre-commit uses a `language: system` hook calling `gitleaks git --pre-commit --staged`; CI downloads the pinned release (8.30.1); `make secrets` fails if gitleaks is missing. Installed locally via Homebrew.
- **Consequence:** contributors need gitleaks on PATH (`brew install gitleaks`).
