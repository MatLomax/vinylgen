set shell := ["bash", "-eu", "-o", "pipefail", "-c"]
# Recipe arguments reach the shell as "$@" too, so they pass through unsplit.
set positional-arguments

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

# Run all automated tests. Backend and frontend run concurrently; each one's
# output is buffered and printed whole so the two never interleave.
test:
    #!/usr/bin/env bash
    set -uo pipefail
    mkdir -p .tmp
    logs="$(mktemp -d .tmp/test-logs.XXXXXX)"
    trap 'rm -rf "$logs"' EXIT
    .venv/bin/python scripts/run_tests.py > "$logs/backend" 2>&1 & backend=$!
    npm --prefix web test > "$logs/frontend" 2>&1 & frontend=$!
    wait "$backend"; backend_status=$?
    wait "$frontend"; frontend_status=$?
    echo "=== backend ==="; cat "$logs/backend"
    echo "=== frontend ==="; cat "$logs/frontend"
    if (( backend_status || frontend_status )); then
        echo "Tests failed: backend exit $backend_status, frontend exit $frontend_status" >&2
        exit 1
    fi

# Runs across one worker per core; pass args such as `-j 1`, `-v`,
# `--durations 20` or test names (e.g. test_web_pipeline.WebCleanupStageTest).
test-backend *args:
    .venv/bin/python scripts/run_tests.py "$@"

test-frontend:
    npm --prefix web test

# Local development servers; run these in separate terminals.
dev-api:
    VINYLGEN_DATA_DIR=.vinylgen-data-dev \
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

# Build, test, install the requirements (the t3api wheel among them), warm the t3
# engine's kernel cache, restart, and check both routes. Refuses to restart while a job is
# active, because the restart would interrupt it and surface a spurious "Worker
# process stopped before completion"; pass force=true to override. just hands a
# recipe parameter its command-line word verbatim, so both "force=true" and a bare
# "true" arrive here and both override.
deploy force="false": test build
    active="$(curl -fsS --max-time 5 http://127.0.0.1:8275/api/health 2>/dev/null \
        | sed -n 's/.*"active_jobs":\([0-9][0-9]*\).*/\1/p' || true)"; \
    if [[ "{{force}}" != "true" && "{{force}}" != "force=true" && -n "$active" && "$active" != "0" ]]; then \
        echo "Refusing to restart: $active job(s) still active; a restart would interrupt them." >&2; \
        echo "Wait for them to finish, or run 'just deploy force=true' to override." >&2; \
        exit 1; \
    fi
    .venv/bin/pip install -r requirements-web.txt
    .venv/bin/python scripts/warm_t3_cache.py
    systemctl --user restart vinylgen
    just health
    just health-public

# Explicit administrative operation, not part of normal deployment. The unit's
# ReadWritePaths names the t3 kernel cache, ~/.cache/t3api, which must exist.
install-services:
    if [[ ! -e .vinylgen-data/vinylgen.env ]]; then \
        install -Dm600 deploy/vinylgen.env .vinylgen-data/vinylgen.env; \
    else \
        echo "Preserving existing .vinylgen-data/vinylgen.env"; \
    fi
    mkdir -p ~/.cache/t3api
    install -Dm644 deploy/vinylgen-user.service \
        ~/.config/systemd/user/vinylgen.service
    install -Dm644 deploy/vinylgen-tunnel-user.service \
        ~/.config/systemd/user/vinylgen-tunnel.service
    systemctl --user daemon-reload
    systemctl --user enable vinylgen.service vinylgen-tunnel.service
    systemctl --user restart vinylgen.service vinylgen-tunnel.service
