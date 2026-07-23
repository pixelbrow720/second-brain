#!/usr/bin/env python3
"""Preflight, apply, or roll back an exact M9 packet.

Every global mutation requires a current exact packet and explicit user approval.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from second_brain.errors import ContractError
from second_brain.global_rollout import (
    apply_m9_packet,
    load_m9_rollback_approval_packet,
    load_m9_packet,
    rollback_m9_packet,
    validate_m9_packet_current,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--packet", default="artifacts/m9-approval-packet.json")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--approval-reference", required=True)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--rollback-approval-packet", required=True)
    rollback_parser.add_argument("--approval-reference", required=True)
    arguments = parser.parse_args()
    try:
        packet = load_m9_packet(arguments.packet)
        if arguments.command == "preflight":
            validate_m9_packet_current(packet, arguments.codex_home)
            result = {"packet_digest": packet["packet_digest"], "status": "PREFLIGHT_PASS"}
        elif arguments.command == "apply":
            result = apply_m9_packet(
                packet,
                arguments.codex_home,
                approval_reference=arguments.approval_reference,
            )
        else:
            rollback_packet = load_m9_rollback_approval_packet(arguments.rollback_approval_packet)
            result = rollback_m9_packet(
                packet,
                arguments.codex_home,
                rollback_approval_packet=rollback_packet,
                approval_reference=arguments.approval_reference,
            )
    except (ContractError, OSError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "reason": type(error).__name__}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
