from __future__ import annotations

import unittest

from second_brain.documentation_checks import run_documentation_checks


class DocumentationChecksTests(unittest.TestCase):
    def test_blueprint_links_and_terms_are_valid(self) -> None:
        self.assertEqual(
            run_documentation_checks(),
            ["docs:local-links", "docs:canonical-terms"],
        )


if __name__ == "__main__":
    unittest.main()
