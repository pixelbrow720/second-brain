#!/usr/bin/env python3
"""Create the immutable, source-bound M8/M9 readiness checkpoint."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import sys

from second_brain.m8_m9_readiness import (
    build_m8_m9_readiness_checkpoint,
    verify_current_m8_m9_readiness_checkpoint,
    write_m8_m9_readiness_checkpoint,
)


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(list(argv))
    try:
        payload = build_m8_m9_readiness_checkpoint()
        write_m8_m9_readiness_checkpoint(payload)
        verified = verify_current_m8_m9_readiness_checkpoint()
    except Exception:
        print(json.dumps({"status": "FAIL", "reason": "M8_M9_READINESS_GENERATION_FAILED"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "checkpoint_digest": verified["checkpoint_digest"],
                "checkpoint_status": verified["checkpoint_status"],
                "promotion_status": verified["promotion_status"],
                "status": "VERIFIED",
            },
            sort_keys=True,
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
