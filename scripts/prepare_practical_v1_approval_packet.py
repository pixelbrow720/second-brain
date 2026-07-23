#!/usr/bin/env python3
"""Create or validate the new exact Practical V1 global approval packet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from second_brain.errors import ContractError
from second_brain.practical_v1_rollout import build_practical_v1_packet, write_practical_v1_packet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--output", default="artifacts/practical-v1-approval-packet.json")
    parser.add_argument("--created-at", default=None)
    parser.add_argument("--replace-existing-packet-digest", default=None)
    arguments = parser.parse_args()
    try:
        packet = build_practical_v1_packet(arguments.codex_home, created_at=arguments.created_at)
        output = write_practical_v1_packet(
            packet,
            arguments.output,
            replace_existing_packet_digest=arguments.replace_existing_packet_digest,
        )
    except (ContractError, OSError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "reason_code": type(error).__name__}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "output": str(output),
                "packet_digest": packet["packet_digest"],
                "status": packet["status"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
