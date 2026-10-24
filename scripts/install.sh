#!/bin/sh
# One-line installer (spec §16.1). Bootstraps uv, then installs this repo as a tool.
set -eu
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
repo=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
uv tool install --force --from "$repo" thememory
echo "Installed. Next: thememory setup"
