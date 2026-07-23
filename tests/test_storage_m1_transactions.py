from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from second_brain.storage import StorageError, StoreHealth

from tests.m1_storage_helpers import (
    OBJECT_A,
    OBJECT_B,
    create_mutation,
    error_code,
    initialized_store,
    make_object,
    open_store,
    replace_mutation,
    transaction,
)


class InjectedCrash(RuntimeError):
    pass


class FailAt:
    def __init__(self, stage: str) -> None:
        self.stage = stage

    def __call__(self, stage: str) -> None:
        if stage == self.stage:
            raise InjectedCrash(stage)


class StorageTransactionTests(unittest.TestCase):
    def assert_error_code(self, expected: str, action: object) -> StorageError:
        with self.assertRaises(StorageError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(error_code(caught.exception), expected)
        return caught.exception

    def test_create_then_replace_enforces_revision_and_content_hash_cas(self) -> None:
        with initialized_store() as (_, _, store):
            original = make_object(OBJECT_A, title="Original title")
            create_receipt = store.commit(
                transaction(
                    "txn:10000000-0000-4000-8000-000000000001",
                    "m1-create-a",
                    [create_mutation(original)],
                )
            )
            self.assertEqual(create_receipt.transaction_id, "txn:10000000-0000-4000-8000-000000000001")
            self.assertEqual(create_receipt.idempotency_key, "m1-create-a")
            self.assertEqual(create_receipt.mutation_epoch, 1)
            self.assertFalse(create_receipt.idempotent)
            self.assertEqual(len(create_receipt.changed_objects), 1)

            current = store.read(OBJECT_A)
            replacement = make_object(
                OBJECT_A,
                title="Replacement title",
                body="A changed, deterministic body.",
                revision=current["revision"] + 1,
            )
            replace_receipt = store.commit(
                transaction(
                    "txn:10000000-0000-4000-8000-000000000002",
                    "m1-replace-a",
                    [replace_mutation(current, replacement)],
                )
            )

            stored = store.read(OBJECT_A)
            self.assertEqual(replace_receipt.mutation_epoch, 2)
            self.assertEqual(stored["revision"], 2)
            self.assertEqual(stored["title"], "Replacement title")
            self.assertEqual(stored["body"], "A changed, deterministic body.")
            self.assertNotEqual(stored["content_hash"], current["content_hash"])

    def test_stale_replace_aborts_without_last_write_wins(self) -> None:
        with initialized_store() as (_, _, store):
            original = make_object(OBJECT_A, title="Original title")
            store.commit(
                transaction(
                    "txn:10000000-0000-4000-8000-000000000003",
                    "m1-stale-create",
                    [create_mutation(original)],
                )
            )
            stale = store.read(OBJECT_A)
            winning = make_object(OBJECT_A, title="Winning write", revision=2)
            store.commit(
                transaction(
                    "txn:10000000-0000-4000-8000-000000000004",
                    "m1-stale-winner",
                    [replace_mutation(stale, winning)],
                )
            )
            losing = make_object(OBJECT_A, title="Losing write", revision=2)

            self.assert_error_code(
                "REVISION_CONFLICT",
                lambda: store.commit(
                    transaction(
                        "txn:10000000-0000-4000-8000-000000000005",
                        "m1-stale-loser",
                        [replace_mutation(stale, losing)],
                    )
                ),
            )
            self.assertEqual(store.read(OBJECT_A)["title"], "Winning write")

    def test_idempotency_receipt_is_durable_and_reused_payload_is_rejected(self) -> None:
        with initialized_store() as (root, clock, store):
            original = make_object(OBJECT_A)
            request = transaction(
                "txn:10000000-0000-4000-8000-000000000006",
                "m1-durable-idempotency",
                [create_mutation(original)],
            )
            first = store.commit(request)
            event_path = root / "events" / "events.ndjson"
            event_bytes = event_path.read_bytes()

            reopened = open_store(root, clock)
            replay = reopened.commit(request)
            self.assertTrue(replay.idempotent)
            self.assertEqual(replay.transaction_id, first.transaction_id)
            self.assertEqual(replay.mutation_epoch, first.mutation_epoch)
            self.assertEqual(event_path.read_bytes(), event_bytes)
            self.assertEqual(reopened.read(OBJECT_A)["revision"], 1)

            self.assert_error_code(
                "IDEMPOTENCY_KEY_REUSED",
                lambda: reopened.commit(
                    transaction(
                        "txn:10000000-0000-4000-8000-000000000007",
                        "m1-durable-idempotency",
                        [create_mutation(make_object(OBJECT_B))],
                    )
                ),
            )
            with self.assertRaises(StorageError):
                reopened.read(OBJECT_B)

    def test_failure_injection_recovers_to_a_whole_pre_or_post_transaction_state(self) -> None:
        for stage in ("after_prepare", "after_objects", "after_events", "after_manifest"):
            with self.subTest(stage=stage), initialized_store() as (root, clock, _):
                failing_store = open_store(root, clock, failure_injector=FailAt(stage))
                request = transaction(
                    "txn:10000000-0000-4000-8000-000000000008",
                    f"m1-crash-{stage}",
                    [
                        create_mutation(make_object(OBJECT_A)),
                        create_mutation(make_object(OBJECT_B)),
                    ],
                )
                with self.assertRaises(InjectedCrash):
                    failing_store.commit(request)

                recovered = open_store(root, clock)
                recovery = recovered.recover()
                self.assertIsNotNone(recovery)
                self.assertEqual(recovered.health, StoreHealth.HEALTHY)

                states: list[dict[str, object] | None] = []
                for object_id in (OBJECT_A, OBJECT_B):
                    try:
                        states.append(recovered.read(object_id))
                    except StorageError:
                        states.append(None)
                self.assertEqual(states[0] is None, states[1] is None)
                if states[0] is not None:
                    self.assertEqual(states[0]["revision"], 1)
                    self.assertEqual(states[1]["revision"], 1)

    def test_concurrent_writers_allow_one_cas_winner_and_one_conflict(self) -> None:
        with initialized_store() as (root, clock, store):
            store.commit(
                transaction(
                    "txn:10000000-0000-4000-8000-000000000009",
                    "m1-concurrent-create",
                    [create_mutation(make_object(OBJECT_A))],
                )
            )
            baseline = store.read(OBJECT_A)
            start = threading.Barrier(2)

            def write(title: str, sequence: str) -> str:
                contender = open_store(root, clock)
                desired = make_object(OBJECT_A, title=title, revision=2)
                request = transaction(
                    f"txn:10000000-0000-4000-8000-0000000000{sequence}",
                    f"m1-concurrent-{sequence}",
                    [replace_mutation(baseline, desired)],
                )
                start.wait(timeout=5)
                try:
                    contender.commit(request)
                except StorageError as error:
                    return error_code(error) or "missing-code"
                return "committed"

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda args: write(*args), (("Writer one", "10"), ("Writer two", "11"))))

            self.assertCountEqual(results, ["committed", "REVISION_CONFLICT"])
            current = open_store(root, clock).read(OBJECT_A)
            self.assertEqual(current["revision"], 2)
            self.assertIn(current["title"], {"Writer one", "Writer two"})


if __name__ == "__main__":
    unittest.main()
