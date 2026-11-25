.PHONY: check lint fmt typecheck imports test cov secrets bench soak up down backtest help

UV ?= uv
RUN = $(UV) run
export PYTHONHASHSEED = 0

help:
	@echo "make check     ruff + pyright + import-linter + pytest + gitleaks (must pass before finishing a task)"
	@echo "make test      pytest (excludes slow soak/bench tests)"
	@echo "make cov       pytest with branch coverage; risk/ must be 100%"
	@echo "make fmt       ruff format + autofix"
	@echo "make bench     non-blocking performance benchmarks (TEST-10)"
	@echo "make up        docker compose up (M4)"
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
		gitleaks git --no-banner --redact . && gitleaks dir --no-banner --redact src tests config docs; \
	else echo "gitleaks not installed: install it (brew install gitleaks) before committing"; exit 1; fi

bench:
	$(RUN) pytest -m slow tests/bench -q

soak:
	$(RUN) helm paper --config config/helm.example.toml --soak-hours $${HOURS:-24}

backtest:
	$(RUN) helm backtest $(ARGS)

up:
	docker compose up -d --build

down:
	docker compose down
