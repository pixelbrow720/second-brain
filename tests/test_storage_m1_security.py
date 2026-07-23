from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import unittest

from second_brain.clock import DeterministicClock
from second_brain.storage import Mutation, StorageError, Store, StoreHealth, TransactionRequest, sha256_hex
from second_brain.workspace import repository_root, temporary_store

from tests.m1_storage_helpers import (
    OBJECT_A,
    OBJECT_B,
    create_mutation,
    error_code,
    initialized_store,
    make_object,
    rehash,
    replace_mutation,
    transaction,
)


PROJECT_ID = "m1-security"
PROJECT_STORE_ID = f"project:{PROJECT_ID}"
PROJECT_DECISION_ID = "mem:m1-security:decision:00000000-0000-4000-8000-000000000031"
PROJECT_TASK_ID = "mem:m1-security:task:00000000-0000-4000-8000-000000000032"


class InjectedCrash(RuntimeError):
    pass


class FailAt:
    def __init__(self, stage: str) -> None:
        self.stage = stage

    def __call__(self, stage: str) -> None:
        if stage == self.stage:
            raise InjectedCrash(stage)


def make_project_object(
    object_id: str,
    *,
    kind: str,
    authority: str = "ai-synthesis",
    revision: int = 1,
    references: list[dict[str, object]] | None = None,
    confirmation_id: str | None = None,
) -> dict[str, object]:
    document = deepcopy(make_object(OBJECT_A))
    document["id"] = object_id
    document["store_id"] = PROJECT_STORE_ID
    document["kind"] = kind
    document["authority"] = authority
    document["revision"] = revision
    document["references"] = references or []
    if confirmation_id is not None:
        document["provenance"] = [
            {
                "provenance_id": "prov:00000000-0000-4000-8000-000000000031",
                "kind": "user_confirmation",
                "observed_at": "2026-07-22T00:00:00Z",
                "actor_id": "root",
                "content_hash": None,
                "ref": confirmation_id,
                "note": "Synthetic confirmation provenance",
            }
        ]
    return rehash(document)


def project_request(
    transaction_id: str,
    idempotency_key: str,
    mutations: list[Mutation],
    *,
    actor: str = "root",
    confirmation: object = None,
    reason: str = "M1 security regression test",
) -> TransactionRequest:
    return TransactionRequest(
        transaction_id=transaction_id,
        idempotency_key=idempotency_key,
        store_id=PROJECT_STORE_ID,
        actor=actor,
        confirmation=confirmation,
        mutations=mutations,
        reason=reason,
    )


def action_digest(request: TransactionRequest) -> str:
    """The confirmation binds semantic intent, not retry transport IDs."""

    return sha256_hex(
        {
            "schema_version": request.schema_version,
            "store_id": request.store_id,
            "actor": request.actor,
            "mutations": [
                {
                    "operation": mutation.operation,
                    "object_id": mutation.object_id,
                    "expected_revision": mutation.expected_revision,
                    "expected_content_hash": mutation.expected_content_hash,
                    "desired_object": mutation.desired_object,
                }
                for mutation in request.mutations
            ],
            "reason": request.reason,
        }
    )


def confirmation_for(request: TransactionRequest, confirmation_id: str) -> dict[str, str]:
    return {
        "confirmation_id": confirmation_id,
        "confirmed_by": "root",
        "confirmed_at": "2026-07-22T00:00:00Z",
        "action_digest": action_digest(request),
    }


class StorageSecurityRegressionTests(unittest.TestCase):
    def assert_error_code(self, expected: str, action: object) -> StorageError:
        with self.assertRaises(StorageError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(error_code(caught.exception), expected)
        return caught.exception

    def _project_store(self, *, authorizer: object = None):
        temporary = temporary_store()
        root = temporary.__enter__() / "project-store"
        clock = DeterministicClock(datetime(2026, 7, 22, tzinfo=UTC))
        store = Store.initialize(root, PROJECT_STORE_ID, clock=clock, authorizer=authorizer)
        self.addCleanup(temporary.__exit__, None, None, None)
        return root, clock, store

    def test_user_decision_requires_root_authorization_digest_and_matching_provenance(self) -> None:
        confirmation_id = "user-confirmation:m1-security-1"
        decision = make_project_object(
            PROJECT_DECISION_ID,
            kind="decision",
            authority="user-decision",
            confirmation_id=confirmation_id,
        )
        mutation = Mutation("create", PROJECT_DECISION_ID, None, None, decision)

        _, _, no_authorizer = self._project_store()
        unsigned = project_request(
            "txn:30000000-0000-4000-8000-000000000001",
            "m1-user-decision-no-authorizer",
            [mutation],
        )
        self.assert_error_code("AUTHORITY_DENIED", lambda: no_authorizer.commit(unsigned))

        _, _, store = self._project_store(authorizer=lambda *args, **kwargs: True)
        forged = project_request(
            "txn:30000000-0000-4000-8000-000000000002",
            "m1-user-decision-forged-digest",
            [mutation],
            confirmation={
                "confirmation_id": confirmation_id,
                "confirmed_by": "root",
                "confirmed_at": "2026-07-22T00:00:00Z",
                "action_digest": "0" * 64,
            },
        )
        self.assert_error_code("CONFIRMATION_INVALID", lambda: store.commit(forged))

        mismatched_provenance = make_project_object(
            PROJECT_DECISION_ID,
            kind="decision",
            authority="user-decision",
            confirmation_id="user-confirmation:other-action",
        )
        provenance_request = project_request(
            "txn:30000000-0000-4000-8000-000000000003",
            "m1-user-decision-provenance-mismatch",
            [Mutation("create", PROJECT_DECISION_ID, None, None, mismatched_provenance)],
        )
        self.assert_error_code(
            "CONFIRMATION_INVALID",
            lambda: store.commit(
                TransactionRequest(
                    transaction_id=provenance_request.transaction_id,
                    idempotency_key=provenance_request.idempotency_key,
                    store_id=provenance_request.store_id,
                    actor=provenance_request.actor,
                    confirmation=confirmation_for(provenance_request, confirmation_id),
                    mutations=provenance_request.mutations,
                    reason=provenance_request.reason,
                )
            ),
        )

        wrong_actor = project_request(
            "txn:30000000-0000-4000-8000-000000000004",
            "m1-user-decision-wrong-actor",
            [mutation],
            actor="agent:not-root",
        )
        self.assert_error_code(
            "CONFIRMATION_INVALID",
            lambda: store.commit(
                TransactionRequest(
                    transaction_id=wrong_actor.transaction_id,
                    idempotency_key=wrong_actor.idempotency_key,
                    store_id=wrong_actor.store_id,
                    actor=wrong_actor.actor,
                    confirmation=confirmation_for(wrong_actor, confirmation_id),
                    mutations=wrong_actor.mutations,
                    reason=wrong_actor.reason,
                )
            ),
        )

        valid_template = project_request(
            "txn:30000000-0000-4000-8000-000000000005",
            "m1-user-decision-valid",
            [mutation],
        )
        valid = TransactionRequest(
            transaction_id=valid_template.transaction_id,
            idempotency_key=valid_template.idempotency_key,
            store_id=valid_template.store_id,
            actor=valid_template.actor,
            confirmation=confirmation_for(valid_template, confirmation_id),
            mutations=valid_template.mutations,
            reason=valid_template.reason,
        )
        store.commit(valid)
        self.assertEqual(store.read(PROJECT_DECISION_ID)["authority"], "user-decision")

    def test_replace_cannot_bypass_lifecycle_or_tombstone_authorization(self) -> None:
        with initialized_store() as (_, _, store):
            original = make_object(OBJECT_A)
            store.commit(
                transaction(
                    "txn:30000000-0000-4000-8000-000000000010",
                    "m1-lifecycle-create",
                    [create_mutation(original)],
                )
            )
            current = store.read(OBJECT_A)
            erased = deepcopy(current)
            erased["revision"] = 2
            erased["lifecycle"] = {
                "status": "deleted",
                "changed_at": "2026-07-22T00:00:00Z",
                "reason": "bypass attempt",
            }
            erased["body"] = ""
            erased = rehash(erased)

            self.assert_error_code(
                "AUTHORITY_DENIED",
                lambda: store.commit(
                    transaction(
                        "txn:30000000-0000-4000-8000-000000000011",
                        "m1-lifecycle-replace-bypass",
                        [replace_mutation(current, erased)],
                    )
                ),
            )
            self.assertEqual(store.read(OBJECT_A)["lifecycle"]["status"], "active")

    def test_project_references_reject_traversal_and_symlink_components(self) -> None:
        def reference(locator: str) -> dict[str, object]:
            return {
                "ref_id": "ref:00000000-0000-4000-8000-000000000031",
                "kind": "file",
                "locator": locator,
                "selector": None,
                "freshness_policy": "exists",
                "captured": {"observed_at": "2026-07-22T00:00:00Z"},
                "required_for": ["m1-security"],
                "optional": False,
            }

        root, _, store = self._project_store()
        traversal = make_project_object(
            PROJECT_TASK_ID,
            kind="task",
            references=[reference("../../outside")],
        )
        self.assert_error_code(
            "PATH_UNSAFE",
            lambda: store.commit(
                project_request(
                    "txn:30000000-0000-4000-8000-000000000020",
                    "m1-reference-traversal",
                    [Mutation("create", PROJECT_TASK_ID, None, None, traversal)],
                )
            ),
        )

        target = root / "reference-target"
        target.mkdir()
        link = root / "reference-link"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"symlink fixtures are unavailable: {error}")
        locator = (link.relative_to(repository_root()) / "nested.txt").as_posix()
        symlinked = make_project_object(
            PROJECT_TASK_ID,
            kind="task",
            references=[reference(locator)],
        )
        self.assert_error_code(
            "PATH_UNSAFE",
            lambda: store.commit(
                project_request(
                    "txn:30000000-0000-4000-8000-000000000021",
                    "m1-reference-symlink",
                    [Mutation("create", PROJECT_TASK_ID, None, None, symlinked)],
                )
            ),
        )

    def test_forged_committed_journal_never_fabricates_idempotency(self) -> None:
        with initialized_store() as (root, clock, _):
            request = transaction(
                "txn:30000000-0000-4000-8000-000000000030",
                "m1-forged-committed-journal",
                [create_mutation(make_object(OBJECT_A))],
            )
            failing = Store.open(root, clock=clock, failure_injector=FailAt("after_prepare"))
            with self.assertRaises(InjectedCrash):
                failing.commit(request)

            journal_path = next((root / "runtime" / "transactions").glob("*.json"))
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["status"] = "committed"
            journal["committed_at"] = "2026-07-22T00:00:00Z"
            journal_path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")), encoding="utf-8")

            reopened = Store.open(root, clock=clock)
            self.assertEqual(reopened.health, StoreHealth.DEGRADED_READ_ONLY)
            self.assert_error_code("STORE_DEGRADED", lambda: reopened.commit(request))

    def test_committed_journal_intent_is_bound_to_its_ledger_block(self) -> None:
        with initialized_store() as (root, clock, store):
            original = transaction(
                "txn:30000000-0000-4000-8000-000000000040",
                "m1-journal-intent-binding",
                [create_mutation(make_object(OBJECT_A))],
            )
            store.commit(original)
            alternate = transaction(
                "txn:30000000-0000-4000-8000-000000000041",
                original.idempotency_key,
                [create_mutation(make_object(OBJECT_B))],
            )

            journal_path = next((root / "runtime" / "transactions").glob("*.json"))
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["intent_digest"] = store._intent_digest(alternate)
            journal_path.write_text(
                json.dumps(journal, sort_keys=True, separators=(",", ":")), encoding="utf-8"
            )

            reopened = Store.open(root, clock=clock)
            self.assertEqual(reopened.health, StoreHealth.DEGRADED_READ_ONLY)
            self.assert_error_code("STORE_DEGRADED", lambda: reopened.commit(alternate))

    def test_create_must_start_with_an_active_lifecycle(self) -> None:
        with initialized_store() as (_, _, store):
            deleted = make_object(OBJECT_A)
            deleted["lifecycle"] = {
                "status": "deleted",
                "changed_at": "2026-07-22T00:00:00Z",
                "reason": "attempt to bypass the tombstone operation",
            }
            deleted["body"] = ""
            deleted = rehash(deleted)

            self.assert_error_code(
                "SCHEMA_INVALID",
                lambda: store.commit(
                    transaction(
                        "txn:30000000-0000-4000-8000-000000000042",
                        "m1-create-deleted-object",
                        [create_mutation(deleted)],
                    )
                ),
            )

    def test_degraded_store_does_not_expose_unrecovered_objects(self) -> None:
        with initialized_store() as (root, clock, _):
            request = transaction(
                "txn:30000000-0000-4000-8000-000000000043",
                "m1-degraded-read-visibility",
                [create_mutation(make_object(OBJECT_A))],
            )
            failing = Store.open(root, clock=clock, failure_injector=FailAt("after_objects"))
            with self.assertRaises(InjectedCrash):
                failing.commit(request)

            reopened = Store.open(root, clock=clock)
            self.assertEqual(reopened.health, StoreHealth.DEGRADED_READ_ONLY)
            self.assert_error_code("STORE_DEGRADED", lambda: reopened.read(OBJECT_A))

            recovery = reopened.recover()
            self.assertEqual(recovery.status, "recovered")
            self.assert_error_code("OBJECT_NOT_FOUND", lambda: reopened.read(OBJECT_A))

    def test_group_or_other_writable_store_root_is_rejected(self) -> None:
        with initialized_store() as (root, clock, _):
            original_mode = os.stat(root).st_mode
            try:
                os.chmod(root, 0o777)
                with self.assertRaises(StorageError) as caught:
                    Store.open(root, clock=clock)
                self.assertEqual(error_code(caught.exception), "PATH_UNSAFE")
            finally:
                os.chmod(root, original_mode)


if __name__ == "__main__":
    unittest.main()
