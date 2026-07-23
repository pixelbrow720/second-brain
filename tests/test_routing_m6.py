from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from second_brain.admission import Lane
from second_brain.errors import SemanticValidationError
from second_brain.jsonio import load_strict_json
from second_brain.profiles import APPROVED_PROFILE_ALIASES, load_profile_registry, resolve_profile
from second_brain.routing import (
    ReconciliationStatus,
    RouteDisposition,
    RouteIntent,
    RouteReceipt,
    SyntheticCanaryResult,
    create_route_receipt,
    reconcile_route,
    retry_allowed,
    route_disposition,
    run_five_repeat_synthetic_canary,
    run_synthetic_canary,
    serialize_route,
    side_effect_allowed,
    synthetic_observation,
)
from second_brain.workspace import repository_root


TIMESTAMP = "2026-07-23T00:00:00Z"


class RoutingM6Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_profile_registry()

    def _route(self, alias: str = "tera-high", suffix: str = "base"):
        intent = RouteIntent.create(
            route_id=f"route:{suffix}",
            task_id=f"task:{suffix}",
            node_id=f"node:{suffix}",
            profile_alias=alias,
            correlation_id=f"corr:{suffix}",
            created_at=TIMESTAMP,
            registry=self.registry,
        )
        serialized = serialize_route(intent, registry=self.registry, sent_at=TIMESTAMP)
        return intent, serialized

    def test_all_aliases_resolve_and_preserve_the_three_effort_values(self) -> None:
        observed_efforts: set[str] = set()
        for alias in APPROVED_PROFILE_ALIASES:
            with self.subTest(alias=alias):
                intent, serialized = self._route(alias, alias)
                resolved = resolve_profile(alias, self.registry)
                self.assertEqual(serialized.raw_model_slug, resolved.raw_model_slug)
                self.assertEqual(serialized.serialized_effort_value, resolved.effort_intent)
                self.assertEqual(serialized.serialized_effort_field, "model_reasoning_effort")
                self.assertEqual(intent.effort_intent, resolved.effort_intent)
                observed_efforts.add(serialized.serialized_effort_value)
        self.assertEqual(observed_efforts, {"high", "max", "xhigh"})

    def test_route_intent_is_strict_and_snapshot_bound(self) -> None:
        intent, serialized = self._route()
        with self.assertRaises(SemanticValidationError):
            replace(intent, effort_intent="xhigh")
        with self.assertRaises(SemanticValidationError):
            RouteIntent.create(
                route_id="route:bad",
                task_id="task:bad",
                node_id="node:bad",
                profile_alias="not-approved",
                correlation_id="corr:bad",
                created_at=TIMESTAMP,
                registry=self.registry,
            )
        changed = deepcopy(self.registry)
        changed["profiles"]["tera-high"]["adapters"]["nine_router"]["raw_model_slug"] = "cx/gpt-5.6-sol"
        with self.assertRaises(SemanticValidationError):
            serialize_route(intent, registry=changed, sent_at=TIMESTAMP)
        self.assertEqual(serialized.raw_model_slug, "cx/gpt-5.6-terra")

    def test_reconciler_has_exact_match_mismatch_missing_ambiguous_and_unsupported_states(self) -> None:
        intent, serialized = self._route()
        observation = synthetic_observation(intent, serialized)
        cases = {
            ReconciliationStatus.MATCH: (observation,),
            ReconciliationStatus.MISMATCH_MODEL: (
                replace(observation, observed_model_slug="cx/gpt-5.6-sol"),
            ),
            ReconciliationStatus.MISMATCH_EFFORT: (
                replace(observation, observed_effort="xhigh"),
            ),
            ReconciliationStatus.MISMATCH_BOTH: (
                replace(
                    observation,
                    observed_model_slug="cx/gpt-5.6-sol",
                    observed_effort="xhigh",
                ),
            ),
            ReconciliationStatus.MISSING_TELEMETRY: (),
            ReconciliationStatus.AMBIGUOUS_TELEMETRY: (observation, observation),
            ReconciliationStatus.UNSUPPORTED_MAPPING: (
                replace(observation, adapter_version="9router/0.5.41"),
            ),
        }
        for expected, observations in cases.items():
            with self.subTest(status=expected.value):
                self.assertEqual(reconcile_route(serialized, observations).status, expected)

        wrong_correlation = replace(observation, route_id="route:foreign")
        self.assertEqual(
            reconcile_route(serialized, (wrong_correlation,)).status,
            ReconciliationStatus.AMBIGUOUS_TELEMETRY,
        )
        incomplete = {"route_id": serialized.route_id, "correlation_id": serialized.correlation_id}
        self.assertEqual(
            reconcile_route(serialized, (incomplete,)).status,
            ReconciliationStatus.AMBIGUOUS_TELEMETRY,
        )
        unknown_normalizer = replace(observation, normalization_rules_version="unknown/1")
        self.assertEqual(
            reconcile_route(serialized, (unknown_normalizer,)).status,
            ReconciliationStatus.UNSUPPORTED_MAPPING,
        )

    def test_historical_non_xhigh_to_xhigh_fixture_fails_every_repeat(self) -> None:
        fixture = load_strict_json(
            repository_root() / "fixtures/m6/route-non-xhigh-observed-xhigh.json"
        )
        statuses: list[ReconciliationStatus] = []
        for repeat in range(5):
            intent = RouteIntent.create(
                route_id=f"{fixture['route_id']}-{repeat}",
                task_id=f"{fixture['task_id']}-{repeat}",
                node_id=f"{fixture['node_id']}-{repeat}",
                profile_alias=fixture["profile_alias"],
                correlation_id=f"{fixture['correlation_id']}-{repeat}",
                created_at=TIMESTAMP,
                registry=self.registry,
            )
            serialized = serialize_route(intent, registry=self.registry, sent_at=TIMESTAMP)
            observation = replace(
                synthetic_observation(intent, serialized),
                observed_effort=fixture["observed_effort"],
            )
            statuses.append(reconcile_route(serialized, (observation,)).status)
        self.assertEqual(statuses, [ReconciliationStatus.MISMATCH_EFFORT] * 5)

    def test_receipts_and_hash_inputs_allowlist_sensitive_input(self) -> None:
        intent, serialized = self._route()
        markers = (
            "M6_SECRET_PROMPT_MARKER",
            "Bearer M6_SECRET_TOKEN",
            "https://private.example.invalid/path?token=M6_SECRET_TOKEN",
            "session=M6_SECRET_COOKIE",
        )
        poison = {
            "route_id": serialized.route_id,
            "correlation_id": serialized.correlation_id,
            "prompt": markers[0],
            "authorization": markers[1],
            "endpoint": markers[2],
            "cookie": markers[3],
        }
        reconciliation = reconcile_route(serialized, (poison,))
        receipt = create_route_receipt(intent, serialized, reconciliation)
        encoded = json.dumps(receipt.to_dict(), sort_keys=True)
        for marker in markers:
            self.assertNotIn(marker, encoded)
            self.assertNotIn(marker, serialized.payload_hash)
        self.assertEqual(reconciliation.status, ReconciliationStatus.AMBIGUOUS_TELEMETRY)
        self.assertFalse(receipt.live_attested)

    def test_synthetic_receipts_never_authorize_graph_or_deep_side_effects(self) -> None:
        intent, serialized = self._route()
        observation = synthetic_observation(intent, serialized)
        records_by_status = {
            ReconciliationStatus.MATCH: (observation,),
            ReconciliationStatus.MISMATCH_MODEL: (
                replace(observation, observed_model_slug="cx/gpt-5.6-sol"),
            ),
            ReconciliationStatus.MISMATCH_EFFORT: (replace(observation, observed_effort="xhigh"),),
            ReconciliationStatus.MISMATCH_BOTH: (
                replace(
                    observation,
                    observed_model_slug="cx/gpt-5.6-sol",
                    observed_effort="xhigh",
                ),
            ),
            ReconciliationStatus.MISSING_TELEMETRY: (),
            ReconciliationStatus.AMBIGUOUS_TELEMETRY: (observation, observation),
            ReconciliationStatus.UNSUPPORTED_MAPPING: (
                replace(observation, adapter_version="9router/0.5.41"),
            ),
        }
        for expected, records in records_by_status.items():
            with self.subTest(status=expected.value):
                reconciliation = reconcile_route(serialized, records)
                receipt = create_route_receipt(intent, serialized, reconciliation)
                self.assertEqual(
                    side_effect_allowed(
                        receipt,
                        Lane.GRAPH,
                        intent=intent,
                        permission_allowed=True,
                    ),
                    False,
                )
                self.assertEqual(
                    side_effect_allowed(
                        receipt,
                        Lane.DEEP,
                        intent=intent,
                        permission_allowed=True,
                    ),
                    False,
                )
                self.assertFalse(
                    side_effect_allowed(receipt, Lane.GRAPH, intent=intent, permission_allowed=False)
                )

        missing = create_route_receipt(
            intent,
            serialized,
            reconcile_route(serialized, ()),
        )
        self.assertEqual(
            route_disposition(missing, Lane.DIRECT, read_only=True),
            RouteDisposition.UNVERIFIED_ROUTE,
        )
        self.assertEqual(
            route_disposition(missing, Lane.GRAPH, read_only=True),
            RouteDisposition.QUARANTINED_ROUTE,
        )
        mismatch = reconcile_route(
            serialized,
            (replace(observation, observed_effort="xhigh"),),
        )
        self.assertTrue(
            retry_allowed(
                mismatch,
                retries_completed=0,
                side_effect_started=False,
                last_known_good_pinned=True,
            )
        )
        self.assertFalse(
            retry_allowed(
                reconcile_route(serialized, ()),
                retries_completed=0,
                side_effect_started=False,
                last_known_good_pinned=True,
            )
        )
        self.assertFalse(
            retry_allowed(
                mismatch,
                retries_completed=1,
                side_effect_started=False,
                last_known_good_pinned=True,
            )
        )
        match_receipt = create_route_receipt(
            intent,
            serialized,
            reconcile_route(serialized, (observation,)),
        )
        changed_registry = deepcopy(self.registry)
        changed_registry["profiles"]["tera-high"]["adapters"]["nine_router"][
            "raw_model_slug"
        ] = "cx/gpt-5.6-sol"
        self.assertFalse(
            side_effect_allowed(
                match_receipt,
                Lane.GRAPH,
                intent=intent,
                permission_allowed=True,
                registry=changed_registry,
            )
        )

    def test_route_receipt_subclass_cannot_spoof_live_attestation(self) -> None:
        intent, serialized = self._route(suffix="subclass-spoof")
        original = create_route_receipt(
            intent,
            serialized,
            reconcile_route(serialized, (synthetic_observation(intent, serialized),)),
        )

        class HostileReceipt(RouteReceipt):
            """Mimic a caller that changes virtual fields after valid construction."""

            def __getattribute__(self, name: str):
                values = object.__getattribute__(self, "__dict__")
                if values.get("_spoof_live_attestation", False):
                    if name == "evidence_scope":
                        return "trusted-live"
                    if name == "live_attested":
                        return True
                return super().__getattribute__(name)

            def to_dict(self) -> dict[str, object]:
                # The old gate reconstructed this valid, original receipt while
                # trusting the spoofed virtual attributes for authorization.
                return object.__getattribute__(self, "__dict__")["_original_receipt"]

        hostile = HostileReceipt(**original.to_dict())
        object.__setattr__(hostile, "_original_receipt", original.to_dict())
        object.__setattr__(hostile, "_spoof_live_attestation", True)
        self.assertEqual(hostile.evidence_scope, "trusted-live")
        self.assertTrue(hostile.live_attested)

        for lane in (Lane.GRAPH, Lane.DEEP):
            with self.subTest(lane=lane):
                self.assertFalse(
                    side_effect_allowed(
                        hostile,
                        lane,
                        intent=intent,
                        permission_allowed=True,
                    )
                )

    def test_canary_requires_all_seven_profiles_and_five_repeats(self) -> None:
        one = run_synthetic_canary(registry=self.registry)
        five = run_five_repeat_synthetic_canary(registry=self.registry)
        self.assertTrue(one.passed)
        self.assertEqual(one.match_count, 7)
        self.assertTrue(five.passed)
        self.assertEqual(five.match_count, 35)

        overrides = {}
        for repeat in range(5):
            intent = RouteIntent.create(
                route_id=f"route:canary-{repeat}-tera-high",
                task_id="task:synthetic-canary",
                node_id="node:tera-high",
                profile_alias="tera-high",
                correlation_id=f"corr:canary-{repeat}-tera-high",
                created_at=TIMESTAMP,
                registry=self.registry,
            )
            serialized = serialize_route(intent, registry=self.registry, sent_at=TIMESTAMP)
            overrides[("tera-high", repeat)] = (
                replace(synthetic_observation(intent, serialized), observed_effort="xhigh"),
            )
        failed = run_five_repeat_synthetic_canary(
            registry=self.registry,
            observation_overrides=overrides,
        )
        self.assertFalse(failed.passed)
        self.assertEqual(
            [
                check.reconciliation.status
                for check in failed.checks
                if check.profile_alias == "tera-high"
            ],
            [ReconciliationStatus.MISMATCH_EFFORT] * 5,
        )

        missing = run_synthetic_canary(
            registry=self.registry,
            observation_overrides={("sol-max", 0): None},
        )
        self.assertFalse(missing.passed)
        self.assertEqual(
            next(check.reconciliation.status for check in missing.checks if check.profile_alias == "sol-max"),
            ReconciliationStatus.MISSING_TELEMETRY,
        )
        foreign_intent = RouteIntent.create(
            route_id="route:canary-0-luna-xhigh",
            task_id="task:synthetic-canary",
            node_id="node:luna-xhigh",
            profile_alias="luna-xhigh",
            correlation_id="corr:canary-0-luna-xhigh",
            created_at=TIMESTAMP,
            registry=self.registry,
        )
        foreign_serialized = serialize_route(
            foreign_intent,
            registry=self.registry,
            sent_at=TIMESTAMP,
        )
        foreign = replace(
            synthetic_observation(foreign_intent, foreign_serialized),
            route_id="route:foreign",
        )
        foreign_result = run_synthetic_canary(
            registry=self.registry,
            observation_overrides={("luna-xhigh", 0): (foreign,)},
        )
        self.assertFalse(foreign_result.passed)
        self.assertFalse(
            SyntheticCanaryResult(repeats=1, checks=one.checks[:-1]).passed,
        )


if __name__ == "__main__":
    unittest.main()
