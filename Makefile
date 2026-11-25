.PHONY: check lint fmt typecheck imports test cov secrets bench soak up down backtest ui help

UV ?= uv
RUN = $(UV) run
export PYTHONHASHSEED = 0

help:
	@echo "make check     ruff + pyright + import-linter + pytest + gitleaks (must pass before finishing a task)"
	@echo "make test      pytest (excludes slow soak/bench tests)"
	@echo "make cov       pytest with branch coverage; risk/ must be 100%"
	@echo "make fmt       ruff format + autofix"
	@echo "make bench     non-blocking performance benchmarks (TEST-10)"
	@echo "make ui        build the static control UI (ui/out)"
	@echo "make up        start the Docker stack via scripts/up.sh (decrypts secrets)"
	@echo "make backtest ARGS=\"...\"   run the event-driven backtester"

check: lint typecheck imports cov secrets

lint:
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests

fmt:
	$(RUN) ruff format src tests
	$(RUN) ruff check --fix src tests

typecheck:
	$(RUN) pyright

imports:
	$(RUN) lint-imports

test:
	$(RUN) pytest -m "not slow"

cov:
	$(RUN) pytest -m "not slow" --cov --cov-branch --cov-report=term-missing:skip-covered
	$(RUN) coverage report --include="src/helm/risk/*" --fail-under=100

secrets:
	@if command -v gitleaks >/dev/null 2>&1; then \
		gitleaks git --no-banner --redact . && \
		for d in src tests config docs scripts ui/app ui/lib; do gitleaks dir --no-banner --redact $$d || exit 1; done; \
	else echo "gitleaks not installed: install it (brew install gitleaks) before committing"; exit 1; fi

bench:
	$(RUN) pytest -m slow tests/bench -q

soak:
	$(RUN) helm paper --config config/helm.example.toml --soak-hours $${HOURS:-24}

backtest:
	$(RUN) helm backtest $(ARGS)

ui:
	cd ui && npm ci --no-audit --no-fund && NEXT_TELEMETRY_DISABLED=1 npx tsc --noEmit && NEXT_TELEMETRY_DISABLED=1 npx next build

up:
	scripts/up.sh

down:
	docker compose down
