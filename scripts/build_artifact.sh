#!/usr/bin/env bash
# Build + smoke-test the paige wheel.
#
# Designed to run inside the dev container; the host calls it via
# `./do.sh artifact`. Validates the wheel against a fresh isolated
# venv, then leaves the verified wheel at /app/dist/ for export.
#
# Smoke checks:
#   1. `paige` script entry installs into the venv's bin/.
#   2. paige.entrypoint.main imports cleanly under dummy env.
#   3. Config.from_env validates a synthetic env without raising.
#   4. Wheel contents audit: required modules present, dev files
#      (tests/, doc/, scripts/, do.sh, Dockerfile.dev) excluded.
#   5. Wheel size sanity: < 1 MB (paige is small; bloat is a regression).
#
# Exits non-zero on any failure. The wheel is NOT promoted to the
# host on failure — `--export` won't copy a broken artifact.

set -euo pipefail

# PROJECT_DIR defaults to /app (the dev-container bind mount); override
# with PROJECT_DIR=$GITHUB_WORKSPACE when running on a GitHub Actions
# runner that doesn't mount the source at /app.
PROJECT_DIR="${PROJECT_DIR:-/app}"
ARTIFACT_VENV="${ARTIFACT_VENV:-/tmp/paige-artifact-venv}"
DIST_DIR="$PROJECT_DIR/dist"
WHEEL_GLOB="$DIST_DIR/paige-*.whl"

echo "[artifact] Building wheel in $DIST_DIR/"
rm -rf "$DIST_DIR" "$ARTIFACT_VENV"
cd "$PROJECT_DIR"
uv build --wheel

# Pick the freshly-built wheel (one match expected).
WHEEL=$(ls -t $WHEEL_GLOB 2>/dev/null | head -n 1)
[ -n "$WHEEL" ] && [ -f "$WHEEL" ] || {
  echo "[artifact] FAIL: wheel not built" >&2
  exit 1
}
echo "[artifact] Built $(basename "$WHEEL")"

echo "[artifact] Installing into fresh venv $ARTIFACT_VENV"
uv venv "$ARTIFACT_VENV" --quiet
# uv venv doesn't ship pip; use `uv pip install` against the venv
# directly (faster, no pip-in-venv needed).
VIRTUAL_ENV="$ARTIFACT_VENV" uv pip install --quiet "${WHEEL}[feishu]"

# 1. paige entry point exists.
[ -x "$ARTIFACT_VENV/bin/paige" ] || {
  echo "[artifact] FAIL: paige script not installed in venv" >&2
  exit 1
}

# 2 + 3. Smoke-import + Config.from_env under dummy env.
PAIGE_FEISHU_APP_ID=dummy-app-id \
  PAIGE_FEISHU_APP_SECRET=dummy-secret \
  PAIGE_DIR=$(mktemp -d) \
  "$ARTIFACT_VENV/bin/python" -c "
from paige.entrypoint.config import Config
import paige.entrypoint.main  # imports composition root; must not crash
cfg = Config.from_env()
assert cfg.feishu_app_id == 'dummy-app-id'
print('[artifact] smoke import ok')
"

# 4. Wheel content audit via Python's zipfile.
"$ARTIFACT_VENV/bin/python" - <<PY
import sys
import zipfile
from pathlib import Path

wheel = Path("$WHEEL")
with zipfile.ZipFile(wheel) as z:
    names = z.namelist()

required = {
    "paige/entrypoint/main.py",
    "paige/entrypoint/config.py",
    "paige/entrypoint/app.py",
    "paige/application/dispatcher.py",
    "paige/application/outbox.py",
    "paige/application/run_registry.py",
    "paige/adapters/tmux.py",
    "paige/adapters/jsonl_watcher.py",
    "paige/adapters/feishu/channel.py",
    "paige/domain/conversation.py",
    "paige/ports/channel.py",
    "paige/application/proc_scan.py",
}
missing = sorted(r for r in required if r not in names)
if missing:
    print(f"[artifact] FAIL: missing from wheel: {missing}", file=sys.stderr)
    sys.exit(1)

# Dev files MUST NOT be in the wheel.
forbidden_prefixes = ("tests/", "doc/", "scripts/", ".github/", ".claude/")
forbidden_files = {"do.sh", "Dockerfile.dev", "PLAN.md", "CLAUDE.md"}
leaked = sorted(
    n for n in names
    if any(n.startswith(p) for p in forbidden_prefixes) or n in forbidden_files
    or n.startswith("paige.testing/")  # the testing package is dev-only
)
if leaked:
    print(f"[artifact] FAIL: dev files leaked into wheel: {leaked}", file=sys.stderr)
    sys.exit(1)

# paige.testing should NOT be packaged.
testing_modules = sorted(n for n in names if "/testing/" in n)
if testing_modules:
    print(f"[artifact] FAIL: paige.testing leaked: {testing_modules}", file=sys.stderr)
    sys.exit(1)

print(f"[artifact] wheel content audit ok ({len(names)} entries)")
PY

# 5. Wheel size sanity (paige is small; protect against accidental bloat).
WHEEL_BYTES=$(stat -c%s "$WHEEL")
WHEEL_KB=$((WHEEL_BYTES / 1024))
MAX_KB=1024
if [ "$WHEEL_KB" -gt "$MAX_KB" ]; then
  echo "[artifact] FAIL: wheel size ${WHEEL_KB}KB exceeds budget ${MAX_KB}KB" >&2
  exit 1
fi
echo "[artifact] wheel size ${WHEEL_KB}KB (budget ${MAX_KB}KB)"

# Standalone wheel checksum — sits next to the wheel for a host
# that wants to verify a single-file install:
#     sha256sum -c paige-X.Y.Z-py3-none-any.whl.sha256
( cd "$DIST_DIR" && sha256sum "$(basename "$WHEEL")" > "$(basename "$WHEEL").sha256" )

# 6. Build the release bundle — wheel + the host-side files a fresh
#    deploy needs. The wheel alone isn't a complete delivery: the
#    target host still needs prod.sh (lifecycle), env.example
#    (config template), and INSTALL.md (operator instructions).
#    A `SHA256SUMS` file inside the bundle lets the recipient verify
#    every file in one `sha256sum -c SHA256SUMS` after extract.
RELEASE_DIR="$DIST_DIR/paige-release"
TARBALL="$DIST_DIR/paige-release.tar.gz"
rm -rf "$RELEASE_DIR" "$TARBALL"
mkdir -p "$RELEASE_DIR"
cp "$WHEEL"                       "$RELEASE_DIR/"
cp "$PROJECT_DIR/scripts/prod.sh" "$RELEASE_DIR/"
cp "$PROJECT_DIR/INSTALL.md"      "$RELEASE_DIR/"
cp "$PROJECT_DIR/env.example"     "$RELEASE_DIR/"
chmod +x "$RELEASE_DIR/prod.sh"

# SHA256SUMS sits inside the bundle; covers every shipped file.
# Generate from inside RELEASE_DIR so the filenames in the manifest
# are bare (no leading dir component) — matches what `sha256sum -c`
# expects to find next to itself.
( cd "$RELEASE_DIR" && sha256sum "$(basename "$WHEEL")" prod.sh INSTALL.md env.example > SHA256SUMS )

# Verify the bundle is shaped the way INSTALL.md says it is.
"$ARTIFACT_VENV/bin/python" - <<PY
import sys
from pathlib import Path

bundle = Path("$RELEASE_DIR")
required = {
    "prod.sh",
    "INSTALL.md",
    "env.example",
    "SHA256SUMS",
}
present = {p.name for p in bundle.iterdir()}
missing = required - present
if missing:
    print(f"[artifact] FAIL: release bundle missing: {sorted(missing)}", file=sys.stderr)
    sys.exit(1)
# Exactly one wheel.
wheels = sorted(bundle.glob("paige-*.whl"))
if len(wheels) != 1:
    print(f"[artifact] FAIL: expected 1 wheel in bundle, got {len(wheels)}", file=sys.stderr)
    sys.exit(1)
# Sanity-check the SHA256SUMS lists every file.
sums = (bundle / "SHA256SUMS").read_text().splitlines()
sum_files = {line.rsplit("  ", 1)[1] for line in sums if "  " in line}
expected = required - {"SHA256SUMS"} | {wheels[0].name}
if sum_files != expected:
    print(f"[artifact] FAIL: SHA256SUMS coverage mismatch: {sum_files} vs {expected}", file=sys.stderr)
    sys.exit(1)
print(f"[artifact] release bundle layout ok ({len(present)} files, SHA256SUMS covers all)")
PY

# Tar it. `--transform` strips the parent dir off the archive entries
# so users untar into `paige-release/` regardless of where the build
# happened.
tar -czf "$TARBALL" -C "$DIST_DIR" "$(basename "$RELEASE_DIR")"
TARBALL_KB=$(($(stat -c%s "$TARBALL") / 1024))
echo "[artifact] release bundle ${TARBALL_KB}KB → $(basename "$TARBALL")"

# Standalone tarball checksum — for a recipient pulling just the
# tarball from a share drop and wanting a single-file integrity
# check before extracting.
( cd "$DIST_DIR" && sha256sum "$(basename "$TARBALL")" > "$(basename "$TARBALL").sha256" )

echo "[artifact] OK: $(basename "$WHEEL") + $(basename "$TARBALL") (+ .sha256 sidecars)"
