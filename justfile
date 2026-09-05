set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

default:
    @just --list

# Install development dependencies.
setup:
    python3 -m venv .venv
    .venv/bin/pip install -r requirements-web.txt
    npm --prefix web ci

# Build the production frontend served by FastAPI.
build:
    npm --prefix web run build

# Run all automated tests.
test: test-backend test-frontend

test-backend:
    .venv/bin/python -m unittest discover -s tests -v

test-frontend:
    npm --prefix web test

# Local development servers; run these in separate terminals.
dev-api:
    VINYLGEN_DATA_DIR=.vinylgen-data \
        .venv/bin/uvicorn vinylgen.web.app:app \
        --host 127.0.0.1 --port 8275 --reload

dev-web:
    npm --prefix web run dev

# Run the deterministic image pipeline.
palette input="images":
    .venv/bin/python palette_infer.py "{{input}}"

trace input="outputs":
    .venv/bin/python svg_trace.py "{{input}}"

pipeline:
    just palette images
    just trace outputs

# Validate the local production service.
health:
    curl -fsS --retry 10 --retry-all-errors --retry-delay 1 --max-time 5 \
        http://127.0.0.1:8275/api/health
    @echo

# The public URL is deployment-specific and lives in the untracked
# .vinylgen-data/vinylgen.env as VINYLGEN_PUBLIC_URL.
health-public:
    url=$(sed -n 's/^VINYLGEN_PUBLIC_URL=//p' .vinylgen-data/vinylgen.env 2>/dev/null || true); \
    if [[ -z "$url" ]]; then \
        echo "VINYLGEN_PUBLIC_URL is not set in .vinylgen-data/vinylgen.env" >&2; exit 1; \
    fi; \
    curl -fsS --retry 10 --retry-all-errors --retry-delay 1 --max-time 5 "$url/api/health"
    @echo

status:
    systemctl --user status vinylgen vinylgen-tunnel --no-pager

logs lines="100":
    journalctl --user \
        -u vinylgen \
        -u vinylgen-tunnel \
        -n "{{lines}}" \
        --no-pager

restart:
    systemctl --user restart vinylgen

# Build, test, restart, and check both routes.
deploy: test build
    systemctl --user restart vinylgen
    just health
    just health-public

# Explicit administrative operation, not part of normal deployment.
install-services:
    if [[ ! -e .vinylgen-data/vinylgen.env ]]; then \
        install -Dm600 deploy/vinylgen.env .vinylgen-data/vinylgen.env; \
    else \
        echo "Preserving existing .vinylgen-data/vinylgen.env"; \
    fi
    install -Dm644 deploy/vinylgen-user.service \
        ~/.config/systemd/user/vinylgen.service
    install -Dm644 deploy/vinylgen-tunnel-user.service \
        ~/.config/systemd/user/vinylgen-tunnel.service
    systemctl --user daemon-reload
    systemctl --user enable vinylgen.service vinylgen-tunnel.service
    systemctl --user restart vinylgen.service vinylgen-tunnel.service
