#!/usr/bin/env python3
"""Generate the fixed, project-local M8 synthetic evidence receipts."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import sys

from second_brain.evaluation import (
    GateState,
    generate_m8_local_evidence,
    load_serialized_receipt,
    verify_m8_local_evidence,
)


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(list(argv))
    try:
        artifacts = generate_m8_local_evidence()
        verification = verify_m8_local_evidence()
        release = load_serialized_receipt("m8-release-report.json")
    except Exception:
        print(
            json.dumps(
                {"status": "FAIL", "evidence_scope": "synthetic-local", "reason": "M8_EVIDENCE_GENERATION_FAILED"},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 1
    status = release["release_status"]
    print(
        json.dumps(
            {
                "status": status,
                "evidence_scope": "synthetic-local",
                "artifacts": [artifact.to_dict() for artifact in artifacts.values()],
                "verification": verification.to_dict(),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    if verification.state is not GateState.PASS or status == "FAIL":
        return 1
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
