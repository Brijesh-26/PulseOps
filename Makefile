# Developer shortcuts. Everything runs locally; nothing calls paid services
# unless PULSEOPS_INVESTIGATOR_PROVIDER=anthropic is set explicitly.
API ?= http://localhost:8000
ADMIN_KEY ?= po_demo_admin_local_only_00001

.PHONY: up down reset logs test lint typecheck check evaluate dashboard-check \
        scenario-a scenario-b scenario-c scenario-d scenario-e scenarios verify-audit

up:            ## start the full local stack (API :8000, dashboard :5173)
	docker compose up -d --build

down:
	docker compose down

reset:         ## wipe local data volumes and restart
	docker compose down -v && docker compose up -d --build

logs:
	docker compose logs -f api worker relay

test:          ## backend tests (needs `docker compose up -d postgres elasticmq`)
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .

typecheck:
	uv run mypy

check: lint typecheck test dashboard-check

dashboard-check:  ## lint, typecheck, test and build the dashboard in a container
	docker run --rm -v "$(CURDIR)/apps/dashboard":/app -w /app node:22-alpine \
	  sh -c "npm ci --no-audit --no-fund && npm run lint && npm run typecheck && npm test && npm run build"

evaluate:      ## run all scenarios on a fresh eval database; writes reports/
	uv run python -m simulator evaluate --out reports

# Load one scenario into the running stack (admin key + demo mode required).
scenario-a scenario-b scenario-c scenario-d scenario-e:
	@curl -sf -X POST "$(API)/api/v1/demo/scenarios/$(subst scenario-,,$@)" -H "X-Api-Key: $(ADMIN_KEY)" && echo

scenarios: scenario-a scenario-b scenario-c scenario-d scenario-e

verify-audit:
	docker compose exec api python -m pulseops.cli verify-audit
