#!/usr/bin/env bash
# Build the benchmark chart gallery and deploy it to
# https://vela.markjy.com/benchmark/ through the rainMark SSH host.
#
# Usage:
#   bash tools/deploy_bench.sh --dry-run
#   bash tools/deploy_bench.sh
#
# Environment overrides:
#   RAINMARK_HOST       SSH host alias/name (default: rainMark)
#   RAINMARK_SITE_ROOT  remote web root (default: /opt/1panel/www/sites/vela.markjy.com/index)
#   BENCH_PUBLIC_URL    public URL prefix (default: https://vela.markjy.com/benchmark)
#   PYTHON              Python interpreter with Pillow (default: python, then python3)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${RAINMARK_HOST:-rainMark}"
SITE_ROOT="${RAINMARK_SITE_ROOT:-/opt/1panel/www/sites/vela.markjy.com/index}"
PUBLIC_URL="${BENCH_PUBLIC_URL:-https://vela.markjy.com/benchmark}"
PUBLIC_URL="${PUBLIC_URL%/}"
BUNDLE="$ROOT/temp/bench_deploy"
ARCHIVE="$ROOT/temp/vela_bench.tgz"
REMOTE_ARCHIVE="/tmp/vela_bench.$$.tgz"
DRY_RUN=0

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  echo "error: $*" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --host) shift; HOST="${1:?--host requires a value}" ;;
    --site-root) shift; SITE_ROOT="${1:?--site-root requires a value}" ;;
    --url) shift; PUBLIC_URL="${1%/}" ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

case "$SITE_ROOT" in
  *"'"*) die "RAINMARK_SITE_ROOT must not contain a single quote" ;;
esac

if [ -z "${PYTHON:-}" ]; then
  if command -v python >/dev/null 2>&1; then
    PYTHON=python
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
  else
    die "Python not found; set PYTHON=/path/to/python"
  fi
fi
"$PYTHON" -c 'import PIL' >/dev/null 2>&1 || \
  die "$PYTHON cannot import Pillow; install Pillow or set PYTHON"

echo "==> build chart bundle"
rm -rf "$BUNDLE"
"$PYTHON" bench/chart.py --out "$BUNDLE" --link-prefix=
test -f "$BUNDLE/index.html" || die "bundle missing index.html"

mkdir -p "$BUNDLE/peers"
for f in \
  bench/peers/results-py3.13.json \
  bench/peers/results-py3.15.json \
  bench/results-py315.json \
  bench/results.json
do
  test -f "$f" || die "missing benchmark data: $f"
  case "$f" in
    bench/peers/*) cp -f "$f" "$BUNDLE/peers/" ;;
    *) cp -f "$f" "$BUNDLE/" ;;
  esac
done

rm -f "$ARCHIVE"
tar -czf "$ARCHIVE" -C "$BUNDLE" .
echo "    bundle: $(wc -c < "$ARCHIVE" | tr -d ' ') bytes, $(find "$BUNDLE" -type f | wc -l | tr -d ' ') files"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> dry-run; upload/switch skipped"
  echo "    local:  $BUNDLE"
  echo "    remote: $HOST:$SITE_ROOT/benchmark"
  exit 0
fi

cleanup() {
  rm -f "$ARCHIVE"
}
trap cleanup EXIT

echo "==> upload to $HOST:$REMOTE_ARCHIVE"
scp -q -o BatchMode=yes "$ARCHIVE" "$HOST:$REMOTE_ARCHIVE"

echo "==> atomic switch on $HOST"
ssh -o BatchMode=yes "$HOST" \
  "SITE_ROOT='$SITE_ROOT' REMOTE_ARCHIVE='$REMOTE_ARCHIVE' bash -s" <<'REMOTE'
set -euo pipefail
cd "$SITE_ROOT"
rm -rf benchmark.new
mkdir -p benchmark.new
tar -xzf "$REMOTE_ARCHIVE" -C benchmark.new
test -f benchmark.new/index.html
if [ -d benchmark ]; then
  rm -rf benchmark.prev
  mv benchmark benchmark.prev
fi
mv benchmark.new benchmark
rm -f "$REMOTE_ARCHIVE"
ls -lh benchmark/index.html
echo "files: $(find benchmark -type f | wc -l | tr -d ' ')"
REMOTE

echo "==> verify $PUBLIC_URL/"
curl -fsS --max-time 30 "$PUBLIC_URL/" -o "$ROOT/temp/bench_index_check.html"
grep -q 'Vela' "$ROOT/temp/bench_index_check.html" || \
  die "deployed index does not look like the Vela chart gallery (previous kept at $SITE_ROOT/benchmark.prev)"
index_bytes=$(wc -c < "$ROOT/temp/bench_index_check.html" | tr -d ' ')

png_bytes=$(curl -fsS --max-time 30 "$PUBLIC_URL/01_empty_call.png" -o /dev/null -w '%{size_download}')
json_bytes=$(curl -fsS --max-time 30 "$PUBLIC_URL/peers/results-py3.15.json" -o /dev/null -w '%{size_download}')

echo "    index: $PUBLIC_URL/ ($index_bytes bytes)"
echo "    png:   $PUBLIC_URL/01_empty_call.png ($png_bytes bytes)"
echo "    json:  $PUBLIC_URL/peers/results-py3.15.json ($json_bytes bytes)"
echo "==> deployed"
