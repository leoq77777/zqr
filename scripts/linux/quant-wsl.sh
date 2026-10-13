#!/usr/bin/env bash
# `quant` entry point inside WSL / Linux. Linked as /usr/local/bin/quant by install.sh, so Windows can call
# `wsl.exe -d Ubuntu-24.04 -- quant ...` without a login shell.
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
export QUANT_HOME="${QUANT_HOME:-$REPO/var}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/mpl-$(id -u)}"
export PATH="$HOME/.local/bin:$PATH"
cd "$REPO"
exec "$REPO/.venv/bin/quant" "$@"
