#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$script_dir/public/app.js" ]; then
  if ! command -v npm >/dev/null 2>&1; then
    printf 'The page needs Node.js 26.13.1 and npm to build. Install Node.js over the vendor route for your platform: the Windows MSI, Homebrew on macOS, or the Linux tarball.\n' >&2
    exit 1
  fi
  printf 'The page is building for the first time. This takes about a minute.\n'
  ( cd "$script_dir" && npm ci && npm run build )
fi

exec python "$script_dir/server.py" "$@"
