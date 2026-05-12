.PHONY: help install dev test lint type smoke clean install-units enable-units \
	uninstall-units enable disable reload restart status stress-smoke health quickstart help-ops hooks release

PY ?= python
VENV ?= .venv
ACT := . $(VENV)/bin/activate &&

SYSTEMD_DST := $(HOME)/.config/systemd/user
UNITS := coolstep-collector.service coolstep-dashboard.service coolstep-bench-gc.service coolstep-bench-gc.timer

help:
	@echo "coolstep — modular predictive soft-cooling"
	@echo ""
	@echo "  make install        venv + editable install"
	@echo "  make dev            install with [dev,nvidia,amd,ml] extras"
	@echo "  make test           pytest with coverage"
	@echo "  make lint           ruff check"
	@echo "  make type           mypy core"
	@echo "  make smoke          60s collector smoke test"
	@echo "  make install-units  copy systemd user units to ~/.config/systemd/user/"
	@echo "  make enable-units   enable+start collector and dashboard units"
	@echo "  make clean          remove caches and venv"
	@echo "  make help-ops       show ops targets (install/enable/restart/status/…)"

install:
	$(PY) -m venv $(VENV)
	$(ACT) pip install --upgrade pip
	$(ACT) pip install -e .

dev:
	$(PY) -m venv $(VENV)
	$(ACT) pip install --upgrade pip
	$(ACT) pip install -e ".[dev,nvidia,amd,ml]"

test:
	$(ACT) pytest --ignore=tests/test_efficiency.py --cov=coolstep --cov-report=term-missing
	$(ACT) pytest tests/test_efficiency.py -q

# pytest + chromadb rust bindings ругаются при concurrent access; efficiency
# тесты используют sqlite напрямую — запускаем их отдельным проходом.

lint:
	$(ACT) ruff check coolstep tests

type:
	$(ACT) mypy

smoke:
	$(ACT) coolstep adapters
	$(ACT) timeout 60 coolstep-collector --smoke || true
	$(ACT) coolstep stats --since 5m

hooks:  ## Install git pre-commit hook
	@./scripts/install-hooks.sh

release:  ## Release new version: make release VERSION=v0.5.0
ifndef VERSION
	@echo "Usage: make release VERSION=v0.5.0" >&2; exit 1
else
	@./scripts/release.sh $(VERSION)
endif

install-units:  ## Copy systemd unit files to ~/.config/systemd/user/
	@mkdir -p $(SYSTEMD_DST)
	@for u in $(UNITS); do \
		if [ -f systemd/$$u ]; then \
			cp -f systemd/$$u $(SYSTEMD_DST)/$$u; \
			echo "  + $$u"; \
		fi; \
	done
	@systemctl --user daemon-reload
	@echo "installed. enable+start: make enable && make restart"

enable-units: install-units
	systemctl --user enable --now coolstep-collector.service
	systemctl --user enable --now coolstep-dashboard.service
	systemctl --user status coolstep-collector.service --no-pager
	systemctl --user status coolstep-dashboard.service --no-pager

# --- coolstep ops targets (P2.5+ era) ---

uninstall-units:  ## Remove systemd unit files from ~/.config/systemd/user/
	@for u in $(UNITS); do \
		if [ -f $(SYSTEMD_DST)/$$u ]; then \
			rm -f $(SYSTEMD_DST)/$$u; \
			echo "  − $$u"; \
		fi; \
	done
	@systemctl --user daemon-reload
	@echo "uninstalled. (drop-ins under .service.d/ remain — remove manually if desired)"

enable:  ## Enable coolstep-collector + coolstep-dashboard
	@systemctl --user enable coolstep-collector.service coolstep-dashboard.service

disable:  ## Disable coolstep-collector + coolstep-dashboard
	@systemctl --user disable coolstep-collector.service coolstep-dashboard.service

reload:  ## systemctl --user daemon-reload
	@systemctl --user daemon-reload

restart:  ## Restart collector + dashboard
	@systemctl --user restart coolstep-collector.service coolstep-dashboard.service
	@sleep 3
	@curl -sf --max-time 3 http://127.0.0.1:18889/api/health >/dev/null \
		&& echo "  ✓ dashboard responding" \
		|| echo "  ✗ dashboard not responding — check journalctl --user -u coolstep-dashboard"

status:  ## Show systemctl is-active + brief journal
	@for u in coolstep-collector coolstep-dashboard; do \
		state=$$(systemctl --user is-active $$u.service 2>/dev/null || echo "?"); \
		echo "  $$u: $$state"; \
	done

stress-smoke:  ## 10s dry-run S1 stress for quick sanity
	@./bench/stress.sh S1 --duration 10 --dry-run

health:  ## Run scripts/health-report.sh
	@./scripts/health-report.sh

quickstart:  ## Full new-host onboarding (calls scripts/quickstart.sh)
	@./scripts/quickstart.sh

help-ops:  ## Show the ops target list
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST) | grep -E "install-units|uninstall|enable|disable|reload|restart|status|stress|health|quickstart"

clean:
	rm -rf $(VENV) build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
