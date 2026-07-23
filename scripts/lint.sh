#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$project_root"
python3 -m compileall -q src
PYTHONPATH="$project_root/src" python3 -m second_brain.contract_checks --lint
