#!/usr/bin/env python3
"""Verify the redacted M9 initial-shadow receipt without making a provider call."""

from __future__ import annotations

import argparse
import json

from second_brain.errors import ContractError
from second_brain.jsonio import load_strict_json
from second_brain.live_shadow import verify_initial_shadow_receipt
from second_brain.workspace import resolve_workspace_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", default="artifacts/m9-initial-shadow-report.json")
    arguments = parser.parse_args()
    try:
        payload = load_strict_json(resolve_workspace_path(arguments.receipt))
        if not isinstance(payload, dict):
            raise ContractError("M9 shadow receipt is not an object")
        verify_initial_shadow_receipt(payload)
        execution = payload["execution"]
        result = {
            "executed_shadow_count": execution["executed_shadow_count"],
            "packet_digest": payload["packet_digest"],
            "passed_shadow_count": execution["passed_shadow_count"],
            "promotion_status": payload["promotion_status"],
            "receipt_digest": payload["receipt_digest"],
            "route_attestation_status": payload["route_attestation"]["status"],
            "shadow_execution_status": payload["shadow_execution_status"],
            "status": "VERIFIED",
        }
    except (ContractError, OSError, ValueError):
        print(json.dumps({"reason": "M9_SHADOW_RECEIPT_INVALID", "status": "FAIL"}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
