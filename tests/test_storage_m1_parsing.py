from __future__ import annotations

import json
import unittest

from second_brain.storage import (
    StorageError,
    canonical_jcs_bytes,
    parse_markdown_object,
    parse_ndjson,
    parse_strict_json,
    sha256_hex,
)

from tests.m1_storage_helpers import fixture_path


class StorageParsingTests(unittest.TestCase):
    def test_safe_frontmatter_subset_parses_mapping_lists_and_flow_values(self) -> None:
        parsed = parse_markdown_object(
            fixture_path("valid-frontmatter.md").read_text(encoding="utf-8")
        )

        self.assertEqual(parsed["title"], "M1 parser fixture")
        self.assertEqual(parsed["tags"], ["m1", "parser"])
        self.assertEqual(parsed["metadata"], {"active": True, "count": 2})
        self.assertEqual(parsed["body"], "This body is data for a deterministic parser fixture.\n")

    def test_line_endings_normalize_before_parser_and_hash_boundary(self) -> None:
        source = fixture_path("valid-frontmatter.md").read_text(encoding="utf-8")
        lf = parse_markdown_object(source)
        crlf = parse_markdown_object(source.replace("\n", "\r\n"))

        self.assertEqual(crlf, lf)
        self.assertNotIn("\r", crlf["body"])
        self.assertEqual(canonical_jcs_bytes(crlf), canonical_jcs_bytes(lf))

    def test_unsafe_or_ambiguous_yaml_features_are_rejected(self) -> None:
        for name in (
            "duplicate-key.md",
            "yaml-anchor.md",
            "yaml-alias.md",
            "yaml-tag.md",
            "multiple-documents.md",
            "malformed-frontmatter.md",
        ):
            with self.subTest(fixture=name):
                text = fixture_path(name).read_text(encoding="utf-8")
                with self.assertRaises(ValueError):
                    parse_markdown_object(text)

    def test_bom_and_missing_or_unterminated_frontmatter_are_rejected(self) -> None:
        valid = fixture_path("valid-frontmatter.md").read_text(encoding="utf-8")
        for text in (
            "\ufeff" + valid,
            "title: no frontmatter\n",
            "---\ntitle: missing terminator\n",
        ):
            with self.subTest(text=text[:24]):
                with self.assertRaises(StorageError):
                    parse_markdown_object(text)

    def test_strict_json_and_ndjson_reject_ambiguous_or_malformed_records(self) -> None:
        self.assertEqual(parse_strict_json('{"answer": 42}'), {"answer": 42})
        self.assertEqual(parse_ndjson('{"one": 1}\n{"two": 2}\n'), [{"one": 1}, {"two": 2}])

        for text in ('{"same": 1, "same": 2}', '{"not_a_number": NaN}'):
            with self.subTest(json=text):
                with self.assertRaises(StorageError):
                    parse_strict_json(text)
        with self.assertRaises(StorageError):
            parse_ndjson('{"valid": true}\nnot-json\n')

    def test_jcs_bytes_and_sha256_match_frozen_rfc8785_style_vector(self) -> None:
        vector = json.loads(fixture_path("jcs-vector.json").read_text(encoding="utf-8"))

        canonical = canonical_jcs_bytes(vector["value"])
        self.assertEqual(canonical.decode("utf-8"), vector["canonical"])
        self.assertEqual(sha256_hex(vector["value"]), vector["sha256"])
        with self.assertRaises(ValueError):
            canonical_jcs_bytes(float("nan"))


if __name__ == "__main__":
    unittest.main()
