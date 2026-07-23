#!/usr/bin/env python3
"""Run the exact approved M9 PUBLIC read-only shadow batch.

This command starts 50 fresh, ephemeral Codex CLI sessions through the fixed
``cx_account`` provider override. It emits and persists redacted evidence only;
it cannot promote the rollout because CLI events do not attest the route.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from second_brain.errors import ContractError
from second_brain.live_shadow import build_and_write_initial_shadow


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--approval-reference", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    arguments = parser.parse_args()
    try:
        result = build_and_write_initial_shadow(
            arguments.codex_home,
            approval_reference=arguments.approval_reference,
            timeout_seconds=arguments.timeout_seconds,
        )
    except (ContractError, OSError, ValueError):
        print(json.dumps({"status": "FAIL", "reason": "M9_SHADOW_FAILED"}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["shadow_execution_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
