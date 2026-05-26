#!/usr/bin/env bash
# Dev tooling for paige. Test-only; no host-side install or live IM.
#
# All commands run inside the `paige-dev` container so the host stays
# untouched. Source is bind-mounted at /app, venv lives in a named
# Docker volume so repeat runs are fast.
#
# Mode:
#   default — bind-mount source from $PROJECT_DIR into /app
#   copy    — persistent sync container (set PAIGE_SYNC_MODE=copy)
#             needed only on docker-in-docker hosts where bind mounts
#             don't reach sibling containers.

set -euo pipefail

IMAGE="${PAIGE_IMAGE:-paige-dev}"
CONTAINER="paige-dev-box"
VENV_VOLUME="paige-venv"
CACHE_VOLUME="paige-uv-cache"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_MODE="${PAIGE_SYNC_MODE:-bind}"
EXTRA_ARGS="${DOCKER_EXTRA_ARGS:-}"

usage() {
  cat <<'EOF'
Usage: ./do.sh <command> [args...]

Dev tooling (runs inside container):
  build          Build the dev image (Dockerfile.dev)
  shell          Interactive bash inside the container
  test [args]    pytest (default: tests/unit only — fast)
  test-all       pytest tests/unit tests/integration tests/e2e
  lint           ruff check + format --check
  type           pyright src/paige/
  layers         import-linter (enforce architectural layers)
  ci             lint + type + layers + test

Artifact (build + smoke-test the wheel inside container):
  artifact [--export <host-dir>]
                 uv build --wheel; install into a fresh venv with
                 [feishu] extra; smoke-test (entry point, imports,
                 wheel content audit). With --export, copy the
                 verified wheel to <host-dir>.

Sync mode (PAIGE_SYNC_MODE=copy for docker-in-docker):
  up             Start persistent dev container
  sync           Copy host source into the container
  down           Stop + remove the container

No host install, no live Feishu. This script is dev-only.
EOF
}

# ── bind-mount mode ──────────────────────────────────────────────────

bind_run() {
  local mode="$1"; shift
  local flags=(--rm)
  [ "$mode" = "it" ] && flags+=(-it)
  # shellcheck disable=SC2086
  docker run "${flags[@]}" $EXTRA_ARGS \
    -v "$PROJECT_DIR":/app \
    -v "$VENV_VOLUME":/app/.venv \
    -v "$CACHE_VOLUME":/root/.cache/uv \
    -w /app \
    "$IMAGE" \
    "$@"
}

# ── persistent container mode ────────────────────────────────────────

copy_up() {
  if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    docker start "$CONTAINER" >/dev/null
  else
    # shellcheck disable=SC2086
    docker run -d $EXTRA_ARGS --name "$CONTAINER" \
      -v "$VENV_VOLUME":/app/.venv \
      -v "$CACHE_VOLUME":/root/.cache/uv \
      -w /app \
      "$IMAGE" \
      sleep infinity >/dev/null
    docker exec "$CONTAINER" mkdir -p /app
  fi
  echo "[do.sh] container ${CONTAINER} up"
}

copy_sync() {
  docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$" || copy_up
  tar -cf - \
    --exclude='.venv' \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='.mypy_cache' \
    -C "$PROJECT_DIR" . \
    | docker exec -i "$CONTAINER" tar -xf - -C /app
}

copy_down() {
  docker rm -f "$CONTAINER" 2>/dev/null || true
}

copy_exec() {
  docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$" || copy_up
  copy_sync
  docker exec -w /app "$CONTAINER" "$@"
}

copy_exec_it() {
  docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$" || copy_up
  copy_sync
  docker exec -it -w /app "$CONTAINER" "$@"
}

run_exec() {
  if [ "$SYNC_MODE" = "copy" ]; then copy_exec "$@"; else bind_run "" "$@"; fi
}
run_exec_it() {
  if [ "$SYNC_MODE" = "copy" ]; then copy_exec_it "$@"; else bind_run it "$@"; fi
}

# ── dispatcher ───────────────────────────────────────────────────────

cmd="${1:-help}"
shift || true

# Inline `uv sync --all-extras` before each tool invocation. uv `run`
# alone only syncs main deps, not the `dev` extras that hold ruff /
# pyright / pytest / import-linter — without this the first
# invocation fails with "No such file or directory: ruff". Sync is
# fast on subsequent runs (named-volume venv is already populated).
SYNC_FIRST="uv sync --all-extras --quiet"

artifact_cmd() {
  # Parse --export <dir> if present.
  local export_dir=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --export) export_dir="${2:-}"; shift 2 ;;
      *) echo "[do.sh] unknown artifact arg: $1" >&2; exit 1 ;;
    esac
  done
  run_exec bash -c "$SYNC_FIRST && /app/scripts/build_artifact.sh"
  if [ -n "$export_dir" ]; then
    mkdir -p "$export_dir"
    if [ "$SYNC_MODE" = "copy" ]; then
      # Copy the wheel + the release tarball + their .sha256 sidecars
      # out of the persistent container. The unpacked
      # `paige-release/` directory inside /app/dist/ is build
      # scaffolding — only the tar (with its embedded SHA256SUMS) ships.
      docker exec "$CONTAINER" sh -c \
        "ls /app/dist/paige-*.whl /app/dist/paige-*.whl.sha256 /app/dist/paige-release.tar.gz /app/dist/paige-release.tar.gz.sha256" \
        | while read -r p; do
          docker cp "$CONTAINER:$p" "$export_dir/"
        done
    else
      # Bind-mount mode: dist/ already exists on the host.
      cp -v "$PROJECT_DIR/dist/"paige-*.whl "$export_dir/"
      cp -v "$PROJECT_DIR/dist/"paige-*.whl.sha256 "$export_dir/"
      cp -v "$PROJECT_DIR/dist/paige-release.tar.gz" "$export_dir/"
      cp -v "$PROJECT_DIR/dist/paige-release.tar.gz.sha256" "$export_dir/"
    fi
    echo "[do.sh] exported wheel + release bundle (+ .sha256 sidecars) → $export_dir/"
  fi
}

case "$cmd" in
  build)    docker build -f Dockerfile.dev -t "$IMAGE" "$PROJECT_DIR" ;;
  shell)    run_exec_it bash ;;
  test)     run_exec bash -c "$SYNC_FIRST && uv run pytest $*" ;;
  test-all) run_exec bash -c "$SYNC_FIRST && uv run pytest tests/unit tests/integration tests/e2e $*" ;;
  lint)     run_exec bash -c "$SYNC_FIRST && uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/" ;;
  type)     run_exec bash -c "$SYNC_FIRST && uv run pyright src/paige/" ;;
  layers)   run_exec bash -c "$SYNC_FIRST && uv run lint-imports" ;;
  ci)       run_exec bash -c "set -e; $SYNC_FIRST && uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run pyright src/paige/ && uv run lint-imports && uv run pytest" ;;
  artifact) artifact_cmd "$@" ;;
  up)       copy_up ;;
  sync)     copy_sync ;;
  down)     copy_down ;;
  help|-h|--help) usage ;;
  *) echo "Unknown command: $cmd" >&2; usage; exit 1 ;;
esac
