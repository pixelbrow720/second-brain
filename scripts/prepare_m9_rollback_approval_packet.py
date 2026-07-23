#!/usr/bin/env python3
"""Create or validate a fresh redacted approval packet for one M9 rollback.

This command reads the current approved post-state but never changes global
Codex files, starts a provider request, or executes a rollback.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from second_brain.errors import ContractError
from second_brain.global_rollout import (
    build_m9_rollback_approval_packet,
    load_m9_packet,
    write_m9_rollback_approval_packet,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--initial-packet", default="artifacts/m9-approval-packet.json")
    parser.add_argument("--output", default="artifacts/m9-rollback-approval-packet.json")
    parser.add_argument("--created-at", default=None)
    parser.add_argument("--replace-existing-packet-digest", default=None)
    arguments = parser.parse_args()
    try:
        initial_packet = load_m9_packet(arguments.initial_packet)
        packet = build_m9_rollback_approval_packet(
            initial_packet,
            arguments.codex_home,
            created_at=arguments.created_at,
        )
        output = write_m9_rollback_approval_packet(
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
                "approval_request": packet["approval_request"],
                "packet_digest": packet["packet_digest"],
                "output": str(output),
                "status": packet["status"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
