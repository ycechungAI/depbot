#!/usr/bin/env bash
# Run depbot from this checkout, so local edits take effect without installing.
#   ./run.sh                     scan every repo in repos.json
#   ./run.sh update my-app       any depbot command
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
if [ $# -eq 0 ]; then set -- scan; fi
exec python3 "$here/depbot.py" "$@"
