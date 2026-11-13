#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$script_dir/dist/bundle/server.py" ]]; then
  exec python "$script_dir/dist/bundle/server.py" "$@"
fi
exec python "$script_dir/server.py" "$@"
