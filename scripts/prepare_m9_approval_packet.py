#!/usr/bin/env python3
"""Create or validate the redacted M9 global approval packet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from second_brain.errors import ContractError
from second_brain.global_rollout import build_m9_packet, write_m9_packet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--output", default="artifacts/m9-approval-packet.json")
    parser.add_argument("--created-at", default=None)
    parser.add_argument("--replace-existing-packet-digest", default=None)
    arguments = parser.parse_args()
    try:
        packet = build_m9_packet(arguments.codex_home, created_at=arguments.created_at)
        output = write_m9_packet(
            packet,
            arguments.output,
            replace_existing_packet_digest=arguments.replace_existing_packet_digest,
        )
    except (ContractError, OSError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "reason": type(error).__name__}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "packet_digest": packet["packet_digest"],
                "output": str(output),
                "status": packet["status"],
            },
            sort_keys=True,
        )
    )
    return 0 if packet["status"] == "PENDING_EXACT_USER_APPROVAL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
