from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from second_brain.activation_v2 import activation_v2_logical_digest
from second_brain.activation_v2_router_shadow import (
    RouteShadowCorpus,
    RouteShadowError,
    RouteShadowReceipt,
    RouteShadowReport,
    evaluate_route_shadow_corpus,
    load_route_shadow_receipt,
    load_route_shadow_report,
    select_shadow_route,
)
from second_brain.activation_v2_runtime import PolicyInputs, initialize_disposable_runtime, runtime_health
from second_brain.contracts import validate_named_document
from second_brain.errors import ContractError, IntegrityError
from second_brain.jsonio import load_strict_json
from second_brain.workspace import repository_root


class ActivationV2A6RouteShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        test_runs = repository_root() / "artifacts" / "test-runs"
        test_runs.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="activation-v2-a6-", dir=test_runs)
        self.sandbox = Path(self.temporary.name)
        self.runtime = self._runtime("runtime")
        self.corpus = load_strict_json(
            repository_root() / "fixtures/canonical/activation-v2-route-shadow-corpus-v1.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _runtime(self, name: str, *, router_entry_point: str = "CLI_SHADOW"):
        return initialize_disposable_runtime(
            self.sandbox / name,
            PolicyInputs.fixture_recommended_defaults(router_entry_point=router_entry_point),
            created_at="2026-07-24T06:00:00Z",
        )

    def _clock(self, case_count: int, *, start: float = 10.0) -> list[float]:
        values: list[float] = []
        for ordinal in range(case_count):
            values.extend((start + ordinal, start + ordinal + 0.0001))
        return values

    def test_canonical_corpus_receipt_and_report_contracts_are_explicitly_shadow_only(self) -> None:
        root = repository_root()
        corpus = load_strict_json(root / "fixtures/canonical/activation-v2-route-shadow-corpus-v1.json")
        receipt = load_strict_json(root / "fixtures/canonical/activation-v2-route-shadow-receipt-v1.json")
        report = load_strict_json(root / "fixtures/canonical/activation-v2-route-shadow-report-v1.json")
        for name, document, model in (
            ("activation-v2-route-shadow-corpus-v1", corpus, RouteShadowCorpus),
            ("activation-v2-route-shadow-receipt-v1", receipt, RouteShadowReceipt),
            ("activation-v2-route-shadow-report-v1", report, RouteShadowReport),
        ):
            with self.subTest(name=name):
                validate_named_document(name, document)
                self.assertEqual(model.from_value(document).to_dict(), document)
        self.assertFalse(receipt["session_created"])
        self.assertFalse(receipt["default_changed"])
        self.assertFalse(receipt["prompt_persisted"])
        self.assertEqual(report["provider_network_calls"], 0)

    def test_fixed_structured_corpus_matches_rules_and_persists_only_redacted_shadow_evidence(self) -> None:
        expected_report = load_strict_json(
            repository_root() / "fixtures/canonical/activation-v2-route-shadow-report-v1.json"
        )
        with patch(
            "second_brain.activation_v2_router_shadow.time.monotonic",
            side_effect=self._clock(len(self.corpus["intents"])),
        ):
            report = evaluate_route_shadow_corpus(
                self.runtime,
                self.corpus,
                report_id="route-shadow-report:10000000-0000-4000-8000-000000000620",
            )
        self.assertEqual(report.to_dict(), expected_report)
        self.assertEqual(load_route_shadow_report(self.runtime, report.report_id).to_dict(), expected_report)
        self.assertEqual(report.fallback_count, 3)
        self.assertEqual(report.max_observed_latency_ms, 1)
        for binding in report.receipt_bindings:
            receipt = load_route_shadow_receipt(self.runtime, binding.receipt_id)
            self.assertEqual(receipt.match_status, "MATCH")
            self.assertFalse(receipt.session_created)
            self.assertFalse(receipt.default_changed)
            self.assertFalse(receipt.prompt_persisted)
            self.assertEqual(receipt.provider_network_calls, 0)
            self.assertNotIn('"prompt":', json.dumps(receipt.to_dict(), sort_keys=True))
        self.assertFalse((self.runtime.root / "projects" / "authority.json").exists())
        self.assertEqual(runtime_health(self.runtime)["authority_store_opened"], False)
        self.assertEqual(runtime_health(self.runtime)["global_mutation"], False)

    def test_rule_corpus_covers_explicit_safe_profile_safety_override_luna_gate_and_uncertainty_fallback(self) -> None:
        decisions = [select_shadow_route(intent) for intent in self.corpus["intents"]]
        self.assertEqual(
            [decision.profile_alias for decision in decisions],
            [intent["expected_profile_alias"] for intent in self.corpus["intents"]],
        )
        self.assertEqual(decisions[3].profile_alias, "luna-xhigh")
        self.assertEqual(decisions[4].selection_reason, "fallback_uncertainty")
        self.assertEqual(decisions[6].selection_reason, "explicit_user_safe")
        self.assertEqual(decisions[7].selection_reason, "safety_override")
        self.assertIn("LUNA_HARD_GATE_FAILED", decisions[7].reason_codes)

    def test_non_shadow_policy_raw_field_and_latency_overrun_fail_before_receipt_write(self) -> None:
        desktop_runtime = self._runtime("desktop-runtime", router_entry_point="DESKTOP_INTEGRATION")
        with self.assertRaises(RouteShadowError):
            evaluate_route_shadow_corpus(desktop_runtime, self.corpus)
        self.assertFalse((desktop_runtime.root / "receipts" / "route-shadow").exists())

        marker = "V2_RAW_TRANSCRIPT_SENTINEL"
        raw = deepcopy(self.corpus)
        raw["intents"][0]["prompt"] = marker
        raw["corpus_digest"] = activation_v2_logical_digest(raw, "corpus_digest")
        with self.assertRaises(ContractError) as raised:
            evaluate_route_shadow_corpus(self.runtime, raw)
        self.assertNotIn(marker, str(raised.exception))
        self.assertFalse((self.runtime.root / "receipts" / "route-shadow").exists())

        with patch(
            "second_brain.activation_v2_router_shadow.time.monotonic",
            side_effect=(10.0, 11.001),
        ):
            with self.assertRaises(RouteShadowError):
                evaluate_route_shadow_corpus(self.runtime, self.corpus)
        self.assertFalse((self.runtime.root / "receipts" / "route-shadow").exists())

    def test_profile_mismatch_is_visible_but_corpus_report_fails_closed(self) -> None:
        mismatch = deepcopy(self.corpus)
        mismatch["intents"][0]["expected_profile_alias"] = "sol-xhigh"
        mismatch["corpus_digest"] = activation_v2_logical_digest(mismatch, "corpus_digest")
        with patch(
            "second_brain.activation_v2_router_shadow.time.monotonic",
            side_effect=self._clock(len(mismatch["intents"])),
        ):
            with self.assertRaises(RouteShadowError):
                evaluate_route_shadow_corpus(self.runtime, mismatch)
        receipts = [load_strict_json(path) for path in (self.runtime.root / "receipts" / "route-shadow").glob("*.json")]
        self.assertTrue(any(item["match_status"] == "MISMATCH_PROFILE" for item in receipts))
        self.assertFalse((self.runtime.root / "receipts" / "route-shadow-reports").exists())

    def test_rehashed_receipt_and_report_boundary_flags_fail_on_readback(self) -> None:
        with patch(
            "second_brain.activation_v2_router_shadow.time.monotonic",
            side_effect=self._clock(len(self.corpus["intents"])),
        ):
            report = evaluate_route_shadow_corpus(
                self.runtime,
                self.corpus,
                report_id="route-shadow-report:10000000-0000-4000-8000-000000000621",
            )
        receipt_id = report.receipt_bindings[0].receipt_id
        receipt_path = self.runtime.root / "receipts" / "route-shadow" / (
            receipt_id.removeprefix("route-shadow-receipt:") + ".json"
        )
        receipt = load_strict_json(receipt_path)
        receipt["provider_network_calls"] = 1
        receipt["receipt_digest"] = activation_v2_logical_digest(receipt, "receipt_digest")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        receipt_path.chmod(0o600)
        with self.assertRaises(IntegrityError):
            load_route_shadow_receipt(self.runtime, receipt_id)

        report_path = self.runtime.root / "receipts" / "route-shadow-reports" / (
            report.report_id.removeprefix("route-shadow-report:") + ".json"
        )
        payload = load_strict_json(report_path)
        payload["sessions_created"] = 1
        payload["report_digest"] = activation_v2_logical_digest(payload, "report_digest")
        report_path.write_text(json.dumps(payload), encoding="utf-8")
        report_path.chmod(0o600)
        with self.assertRaises(IntegrityError):
            load_route_shadow_report(self.runtime, report.report_id)


if __name__ == "__main__":
    unittest.main()
