#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$project_root/var/tools:$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  printf '需要 uv 来运行隔离的 Python 配置向导。可只安装到本仓库 var/tools。\n'
  [[ -t 0 ]] || { echo '请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/' >&2; exit 1; }
  read -r -p '从 astral.sh 安装 uv 0.9.0 到本仓库？[y/N] ' install_uv
  [[ "$install_uv" == [yY] ]] || exit 1
  mkdir -p "$project_root/var/tools"
  installer=$(mktemp)
  trap 'rm -f -- "$installer"' EXIT
  curl --proto '=https' --tlsv1.2 -fsSL https://astral.sh/uv/0.9.0/install.sh -o "$installer"
  UV_UNMANAGED_INSTALL="$project_root/var/tools" sh "$installer"
  rm -f -- "$installer"
  trap - EXIT
fi
exec uv run --project "$project_root" --locked --python 3.12 python "$project_root/deploy/main.py" "$@"
