"""Focused M3 acceptance tests for local-only raw source capture."""

from __future__ import annotations

from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
import queue
import unittest
import uuid

from second_brain.clock import DeterministicClock
from second_brain.ingest import (
    CaptureError,
    CaptureRevisionConflictError,
    RawCaptureRepository,
    capture_manifest_content_hash,
    validate_capture_manifest,
)
from second_brain.workspace import repository_root, temporary_store


def _review_capture_in_process(
    root: str,
    capture_id: str,
    release: multiprocessing.synchronize.Event,
    reached_replace: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    """Pause after the CAS read to prove review serialization across processes."""

    repository = RawCaptureRepository(root)
    replace = repository._replace_manifest

    def delayed_replace(path: Path, manifest: object) -> None:
        reached_replace.set()
        if not release.wait(10):
            raise RuntimeError("review test release timed out")
        replace(path, manifest)  # type: ignore[arg-type]

    repository._replace_manifest = delayed_replace  # type: ignore[method-assign]
    try:
        reviewed = repository.review(capture_id, "accept", expected_revision=1)
        results.put(("ok", reviewed["revision"]))
    except CaptureError as error:
        results.put((error.code, None))
    except BaseException as error:  # pragma: no cover - surfaced by the parent assertion.
        results.put((f"unexpected:{type(error).__name__}", None))


class RawCaptureM3Tests(unittest.TestCase):
    """Lock the raw evidence boundary to repository-contained synthetic fixtures."""

    def fixture(self, name: str) -> bytes:
        return (repository_root() / "fixtures" / "m3" / "capture" / name).read_bytes()

    def request(
        self,
        *,
        locator: str = "fixture:m3-capture",
        kind: str = "manual",
        media_type: str = "text/plain",
        license_name: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "origin": {"kind": kind, "locator": locator},
            "media_type": media_type,
            "retention": {"classification": "public", "expires_at": None},
            "captured_by": "agent:m3-capture-test",
        }
        if license_name is not None:
            result["license"] = license_name
        return result

    def repository(self, root: Path, **policy: object) -> RawCaptureRepository:
        return RawCaptureRepository(
            root,
            clock=DeterministicClock(),
            policy=policy or None,
        )

    def assert_error_code(self, expected: str, action: object) -> CaptureError:
        with self.assertRaises(CaptureError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(caught.exception.code, expected)
        return caught.exception

    def test_identical_bytes_deduplicate_blob_but_retain_capture_provenance(self) -> None:
        payload = self.fixture("safe-source.txt")
        with temporary_store() as temporary_root:
            repository = self.repository(temporary_root / "capture-store")
            first = repository.capture_bytes(
                {**self.request(locator="fixture:one"), "content": payload}
            )
            second = repository.capture_bytes(
                {**self.request(locator="fixture:two"), "content": payload}
            )

            self.assertNotEqual(first.capture_id, second.capture_id)
            self.assertEqual(first.content_digest, second.content_digest)
            self.assertFalse(first.blob_existed)
            self.assertTrue(second.blob_existed)
            self.assertEqual(first.manifest["origin"]["locator"], "fixture:one")
            self.assertEqual(second.manifest["origin"]["locator"], "fixture:two")
            blob_path = temporary_root / "capture-store" / first.manifest["blob"]["relative_path"]
            self.assertEqual(blob_path.read_bytes(), payload)
            self.assertEqual(len(list((temporary_root / "capture-store" / "raw" / "blobs").rglob("*"))), 3)
            self.assertEqual(len(repository.list_manifests()), 2)

    def test_changed_bytes_create_immutable_separate_blobs(self) -> None:
        original = self.fixture("safe-source.txt")
        changed = original + b"Changed synthetic byte sequence.\n"
        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            repository = self.repository(root)
            first = repository.capture_bytes(self.request(), original)
            second = repository.capture_bytes(self.request(locator="fixture:changed"), changed)

            self.assertNotEqual(first.content_digest, second.content_digest)
            old_blob = root / first.manifest["blob"]["relative_path"]
            new_blob = root / second.manifest["blob"]["relative_path"]
            self.assertEqual(old_blob.read_bytes(), original)
            self.assertEqual(new_blob.read_bytes(), changed)
            self.assertNotEqual(old_blob, new_blob)
            self.assertEqual(first.manifest["revision"], 1)
            self.assertEqual(second.manifest["revision"], 1)

    def test_policy_gates_are_deterministic_and_redacted(self) -> None:
        instruction = self.fixture("instruction-source.txt")
        malformed = self.fixture("malformed.json")
        synthetic_marker = self.fixture("secret-marker.txt")
        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            repository = self.repository(root, max_bytes=8)

            oversized = repository.capture_bytes(self.request(), b"synthetic oversize input")
            self.assertEqual(oversized.manifest["status"], "rejected")
            self.assertEqual(oversized.manifest["scan"]["reason_codes"], ["SIZE_EXCEEDED"])
            self.assertFalse((root / oversized.manifest["blob"]["relative_path"]).exists())

            secret = repository.capture_bytes(self.request(locator="fixture:detector"), synthetic_marker)
            self.assertEqual(secret.manifest["status"], "rejected")
            self.assertEqual(secret.manifest["scan"]["secret"], "suspected")
            self.assertIn("SECRET_SUSPECTED", secret.manifest["scan"]["reason_codes"])
            self.assertFalse((root / secret.manifest["blob"]["relative_path"]).exists())

            restricted = repository.capture_bytes(
                self.request(locator="fixture:license", license_name="restricted"),
                b"ordinary synthetic evidence",
            )
            self.assertEqual(restricted.manifest["status"], "rejected")
            self.assertIn("LICENSE_RESTRICTED", restricted.manifest["scan"]["reason_codes"])

            unsafe_url = repository.capture_bytes(
                self.request(kind="url", locator="https://user@example.invalid/source"),
                b"ordinary synthetic evidence",
            )
            self.assertEqual(unsafe_url.manifest["status"], "rejected")
            self.assertEqual(unsafe_url.manifest["origin"]["locator"], "redacted:url-unsafe")
            self.assertIn("URL_UNSAFE", unsafe_url.manifest["scan"]["reason_codes"])

            unsafe_path = repository.capture_bytes(
                self.request(kind="file", locator="../synthetic-outside.txt"),
                b"ordinary synthetic evidence",
            )
            self.assertEqual(unsafe_path.manifest["status"], "rejected")
            self.assertEqual(unsafe_path.manifest["origin"]["locator"], "redacted:path-unsafe")
            self.assertIn("PATH_UNSAFE", unsafe_path.manifest["scan"]["reason_codes"])

            # A separate repository avoids the deliberately tight size policy.
            normal = self.repository(temporary_root / "normal-capture-store")
            unsupported = normal.capture_bytes(
                self.request(media_type="application/pdf"),
                instruction,
            )
            self.assertEqual(unsupported.manifest["status"], "quarantined")
            self.assertEqual(unsupported.manifest["scan"]["format"], "unsupported")
            self.assertIn("FORMAT_UNSUPPORTED", unsupported.manifest["scan"]["reason_codes"])

            malformed_result = normal.capture_bytes(
                self.request(media_type="application/json"),
                malformed,
            )
            self.assertEqual(malformed_result.manifest["status"], "quarantined")
            self.assertEqual(malformed_result.manifest["scan"]["format"], "malformed")
            self.assertIn("FORMAT_MALFORMED", malformed_result.manifest["scan"]["reason_codes"])

    def test_instruction_like_text_remains_quarantined_data_until_explicit_review(self) -> None:
        payload = self.fixture("instruction-source.txt")
        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            repository = self.repository(root)
            capture = repository.capture_bytes(self.request(), payload)

            self.assertEqual(capture.manifest["status"], "quarantined")
            self.assertEqual(capture.manifest["scan"]["injection"], "contains_instruction_like_text")
            self.assertEqual(capture.manifest["scan"]["decision"], "quarantine")
            self.assertEqual(
                (root / capture.manifest["blob"]["relative_path"]).read_bytes(),
                payload,
            )

            reviewed = repository.review(
                capture.capture_id,
                {"decision": "accept", "expected_revision": 1},
            )
            self.assertEqual(reviewed["status"], "accepted")
            self.assertEqual(reviewed["revision"], 2)
            self.assertEqual(reviewed["scan"]["injection"], "contains_instruction_like_text")
            self.assertIsNone(reviewed["source_object_id"])

    def test_review_uses_compare_and_swap_and_preserves_blob_bytes(self) -> None:
        payload = self.fixture("malformed.json")
        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            repository = self.repository(root)
            capture = repository.capture_bytes(self.request(media_type="application/json"), payload)
            old_hash = str(capture.manifest["content_hash"])
            blob = root / capture.manifest["blob"]["relative_path"]

            self.assert_error_code(
                "REVISION_CONFLICT",
                lambda: repository.review(
                    capture.capture_id,
                    "accept",
                    expected_revision=2,
                ),
            )
            updated = repository.review(
                capture.capture_id,
                "reject",
                expected_revision=1,
                expected_content_hash=old_hash,
            )
            self.assertEqual(updated["status"], "rejected")
            self.assertEqual(updated["revision"], 2)
            self.assertNotEqual(updated["content_hash"], old_hash)
            self.assertEqual(blob.read_bytes(), payload)
            with self.assertRaises(CaptureRevisionConflictError):
                repository.review(capture.capture_id, "accept", expected_revision=1)

    def test_capture_file_requires_one_safe_contained_regular_file(self) -> None:
        with temporary_store() as temporary_root:
            input_root = temporary_root / "input"
            input_root.mkdir()
            source = input_root / "source.txt"
            source.write_bytes(self.fixture("safe-source.txt"))
            repository = self.repository(temporary_root / "capture-store")
            request = self.request(kind="file", locator="source.txt")

            captured = repository.capture_file("source.txt", request, allowed_root=input_root)
            self.assertEqual(captured.manifest["status"], "accepted")
            self.assert_error_code(
                "PATH_UNSAFE",
                lambda: repository.capture_file("../source.txt", request, allowed_root=input_root),
            )
            self.assert_error_code(
                "PATH_UNSAFE",
                lambda: repository.capture_file(source, request, allowed_root=input_root),
            )
            self.assert_error_code(
                "PATH_UNSAFE",
                lambda: repository.capture_file("source.txt", request),
            )
            self.assert_error_code(
                "PATH_UNSAFE",
                lambda: repository.capture_file(".", request, allowed_root=input_root),
            )

            outside = temporary_root / "outside"
            outside.mkdir()
            (outside / "source.txt").write_bytes(b"outside synthetic source")
            link = input_root / "linked"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlink fixture unavailable: {error}")
            self.assert_error_code(
                "PATH_UNSAFE",
                lambda: repository.capture_file("linked/source.txt", request, allowed_root=input_root),
            )

    def test_capture_root_must_remain_inside_the_project_staging_boundary(self) -> None:
        outside = repository_root().parent / "m3-outside-capture-root"
        self.assert_error_code("PATH_UNSAFE", lambda: RawCaptureRepository(outside))

    def test_manifest_validation_rejects_hash_tampering_and_uses_schema_contract(self) -> None:
        with temporary_store() as temporary_root:
            repository = self.repository(temporary_root / "capture-store")
            capture = repository.capture_bytes(self.request(), self.fixture("safe-source.txt"))
            manifest = repository.get_manifest(capture.capture_id)

            validate_capture_manifest(manifest)
            self.assertEqual(manifest["content_hash"], capture_manifest_content_hash(manifest))
            tampered = deepcopy(manifest)
            tampered["content_hash"] = "0" * 64
            self.assert_error_code("MANIFEST_HASH_INVALID", lambda: validate_capture_manifest(tampered))

            path = temporary_root / "capture-store" / "raw" / "manifests" / f"{capture.capture_id}.json"
            path.write_text('{"not":"a manifest"}', encoding="utf-8")
            self.assert_error_code("MANIFEST_INVALID", lambda: repository.get_manifest(capture.capture_id))

    def test_review_cas_is_serialized_across_independent_processes(self) -> None:
        payload = self.fixture("instruction-source.txt")
        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            capture = self.repository(root).capture_bytes(self.request(), payload)
            self.assertEqual(capture.manifest["status"], "quarantined")
            context = multiprocessing.get_context("spawn")
            release = context.Event()
            reached_replace = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_review_capture_in_process,
                    args=(str(root), capture.capture_id, release, reached_replace, results),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            self.assertTrue(reached_replace.wait(10), "no child reached the review write barrier")
            release.set()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            outcomes = []
            for _ in processes:
                try:
                    outcomes.append(results.get(timeout=5))
                except queue.Empty as error:  # pragma: no cover - assertion below gives the useful failure.
                    raise AssertionError("review child did not report an outcome") from error
            self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["REVISION_CONFLICT", "ok"])
            self.assertEqual(self.repository(root).get_manifest(capture.capture_id)["revision"], 2)

    def test_history_binds_manifest_filename_state_and_blob_size(self) -> None:
        payload = self.fixture("instruction-source.txt")
        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            repository = self.repository(root)
            capture = repository.capture_bytes(self.request(), payload)
            manifest_path = root / "raw" / "manifests" / f"{capture.capture_id}.json"

            moved_id = f"cap:{uuid.uuid4()}"
            os.replace(manifest_path, manifest_path.with_name(f"{moved_id}.json"))
            self.assert_error_code("INTEGRITY_FAILED", lambda: repository.get_manifest(moved_id))

        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            repository = self.repository(root)
            capture = repository.capture_bytes(self.request(), payload)
            manifest_path = root / "raw" / "manifests" / f"{capture.capture_id}.json"
            forged = json.loads(manifest_path.read_text(encoding="utf-8"))
            forged["status"] = "accepted"
            forged["scan"]["decision"] = "accept"
            forged["content_hash"] = capture_manifest_content_hash(forged)
            manifest_path.write_text(json.dumps(forged), encoding="utf-8")
            self.assert_error_code("INTEGRITY_FAILED", lambda: repository.get_manifest(capture.capture_id))

        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            repository = self.repository(root)
            capture = repository.capture_bytes(self.request(), self.fixture("safe-source.txt"))
            blob = root / capture.manifest["blob"]["relative_path"]
            blob.write_bytes(blob.read_bytes() + b"extra byte")
            self.assert_error_code("BLOB_INTEGRITY_FAILED", lambda: repository.get_manifest(capture.capture_id))

        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            repository = self.repository(root)
            capture = repository.capture_bytes(self.request(), self.fixture("safe-source.txt"))
            blob = root / capture.manifest["blob"]["relative_path"]
            tampered = bytearray(blob.read_bytes())
            tampered[0] ^= 0x01
            blob.write_bytes(bytes(tampered))
            self.assert_error_code("BLOB_INTEGRITY_FAILED", lambda: repository.get_manifest(capture.capture_id))

        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            repository = self.repository(root)
            capture = repository.capture_bytes(self.request(), payload)
            repository.review(capture.capture_id, "accept", expected_revision=1)
            events_path = root / "raw" / "events.ndjson"
            lines = events_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            events_path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
            self.assert_error_code("INTEGRITY_FAILED", lambda: repository.get_manifest(capture.capture_id))

        with temporary_store() as temporary_root:
            root = temporary_root / "capture-store"
            repository = self.repository(root)
            capture = repository.capture_bytes(self.request(), payload)
            events_path = root / "raw" / "events.ndjson"
            event = json.loads(events_path.read_text(encoding="utf-8"))
            event["event_hash"] = "0" * 64
            events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            self.assert_error_code("INTEGRITY_FAILED", lambda: repository.get_manifest(capture.capture_id))

    def test_yaml_is_quarantined_and_cannot_be_enabled_without_a_parser(self) -> None:
        with temporary_store() as temporary_root:
            aliases = (
                "application/yaml",
                "application/yml",
                "application/vnd.example+yaml",
                "text/vnd.example+yml",
            )
            for index, media_type in enumerate(aliases):
                with self.subTest(media_type=media_type):
                    repository = self.repository(temporary_root / f"capture-store-{index}")
                    yaml_capture = repository.capture_bytes(
                        self.request(media_type=media_type),
                        b"title: [unterminated\n",
                    )
                    self.assertEqual(yaml_capture.manifest["status"], "quarantined")
                    self.assertEqual(yaml_capture.manifest["scan"]["format"], "unsupported")
                    self.assertIn("FORMAT_UNSUPPORTED", yaml_capture.manifest["scan"]["reason_codes"])
                    self.assert_error_code(
                        "SCHEMA_INVALID",
                        lambda media_type=media_type: self.repository(
                            temporary_root / f"yaml-policy-{index}",
                            allowed_media_types={media_type},
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
