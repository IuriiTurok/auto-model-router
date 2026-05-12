#!/usr/bin/env bash
# install.sh — post-install helper for auto-model-router.
#
# What it does:
#   1. Symlinks bin/cc-route into ~/.local/bin/cc-route so the CLI
#      wrapper is on PATH (assumes ~/.local/bin is on PATH).
#   2. Creates ~/.claude/cache/router/ if missing.
#   3. Prints a verification command you can run.
#
# Safe to re-run: uses `ln -sf`, idempotent mkdir.

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cc_route_src="$PLUGIN_ROOT/bin/cc-route"
target_dir="${HOME}/.local/bin"
target_link="$target_dir/cc-route"

cache_dir="${HOME}/.claude/cache/router"

echo "auto-model-router install.sh"
echo "  plugin root: $PLUGIN_ROOT"

if [ ! -f "$cc_route_src" ]; then
  echo "  ERROR: cc-route not found at $cc_route_src" >&2
  exit 1
fi
chmod +x "$cc_route_src"

mkdir -p "$target_dir"
ln -sf "$cc_route_src" "$target_link"
echo "  symlinked: $target_link -> $cc_route_src"

mkdir -p "$cache_dir"
echo "  ensured:   $cache_dir"

# PATH check — warn if ~/.local/bin isn't on PATH.
case ":$PATH:" in
  *":$target_dir:"*) ;;
  *)
    echo
    echo "  WARN: $target_dir is not on your PATH."
    echo "  Add this to your shell rc file:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

echo
echo "Done. Verify with:"
echo "  cc-route --help"
echo "  cc-route --dry-run \"list open PRs\""
