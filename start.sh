#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$script_dir/public/app.js" ]; then
  printf 'The page files are missing from this bundle. Download the bundle again and unzip the whole folder.\n' >&2
  exit 1
fi

exec python "$script_dir/server.py" "$@"
