#!/usr/bin/env python3
"""Verify the M8/M9 readiness checkpoint without global or provider access."""

from __future__ import annotations

import argparse
import json

from second_brain.errors import ContractError
from second_brain.m8_m9_readiness import (
    M8_M9_READINESS_RECEIPT_NAME,
    verify_current_m8_m9_readiness_checkpoint,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", default=f"artifacts/{M8_M9_READINESS_RECEIPT_NAME}")
    arguments = parser.parse_args()
    try:
        payload = verify_current_m8_m9_readiness_checkpoint(arguments.receipt)
    except (ContractError, OSError, ValueError):
        print(json.dumps({"status": "FAIL", "reason": "M8_M9_READINESS_INVALID"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "checkpoint_digest": payload["checkpoint_digest"],
                "checkpoint_status": payload["checkpoint_status"],
                "promotion_status": payload["promotion_status"],
                "status": "VERIFIED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
