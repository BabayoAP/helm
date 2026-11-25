#!/usr/bin/env bash
# Start the Helm stack with secrets decrypted into this shell's environment only
# (SEC-2). Secrets live encrypted at rest in secrets/helm.env.age; Docker Compose
# turns the variables into /run/secrets/* tmpfs files inside the engine container.
#
#   age-keygen -o ~/.config/helm/age.key                  # once; keep it OFF the server
#   age -r <public key> -o secrets/helm.env.age <<< 'HELM_SECRET_ALPACA_PAPER_KEY_ID=...
#   HELM_SECRET_ALPACA_PAPER_SECRET_KEY=...'
#   BACKUP_AGE_RECIPIENT=age1... scripts/up.sh
set -euo pipefail
cd "$(dirname "$0")/.."
IDENTITY="${HELM_AGE_IDENTITY:-$HOME/.config/helm/age.key}"
SECRETS_FILE="${HELM_SECRETS_FILE:-secrets/helm.env.age}"
[[ -f "$IDENTITY" ]] || { echo "age identity not found: $IDENTITY" >&2; exit 1; }
[[ -f "$SECRETS_FILE" ]] || { echo "encrypted secrets not found: $SECRETS_FILE" >&2; exit 1; }
[[ -f config/helm.toml && -f config/risk.toml ]] || {
  echo "copy config/*.example.toml to config/helm.toml and config/risk.toml first" >&2; exit 1; }
while IFS='=' read -r key value; do
  [[ "$key" =~ ^HELM_SECRET_[A-Z0-9_]+$ ]] || continue
  export "$key=$value"
done < <(age -d -i "$IDENTITY" "$SECRETS_FILE")
mkdir -p data backups
exec docker compose up -d --build "$@"
