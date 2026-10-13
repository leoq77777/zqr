#!/usr/bin/env bash
# Inside WSL (or any Linux): install uv, sync the environment, put `quant` on PATH.
# Run from the repository: bash scripts/linux/install.sh
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
cd "$REPO"

if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  echo "installing uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

uv sync --frozen
chmod +x scripts/linux/quant-wsl.sh

if [ "$(id -u)" = "0" ]; then
  ln -sf "$REPO/scripts/linux/quant-wsl.sh" /usr/local/bin/quant
elif [ -w /usr/local/bin ]; then
  ln -sf "$REPO/scripts/linux/quant-wsl.sh" /usr/local/bin/quant
else
  mkdir -p "$HOME/.local/bin"
  ln -sf "$REPO/scripts/linux/quant-wsl.sh" "$HOME/.local/bin/quant"
  echo "linked ~/.local/bin/quant (Windows setup links /usr/local/bin/quant as root)"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "WARNING: docker CLI not found. Enable Docker Desktop > Settings > Resources > WSL integration for this distro."
fi
echo "installed: $(.venv/bin/quant --help >/dev/null && echo ok)"
