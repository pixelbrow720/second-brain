"""Deterministic link and terminology checks for the frozen blueprint."""

from __future__ import annotations

from pathlib import Path
import re
import sys

from .errors import ContractError
from .workspace import repository_root


_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_EXTERNAL_LINK = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_REQUIRED_TERMS = {
    "docs/05-MODEL-ROUTING.md": (
        "Tera Max",
        "Tera xhigh",
        "Tera high",
        "Sol Max",
        "Sol xhigh",
        "Luna xhigh",
        "GPT-5.5 xhigh",
    ),
    "docs/06-DATA-SCHEMAS.md": (
        "fresh",
        "partial",
        "stale",
        "unverifiable",
        "not_applicable",
        "content_hash",
        "store_id",
    ),
    "docs/09-IMPLEMENTATION-ROADMAP.md": (
        "M0 - Contract Freeze and Local Scaffold",
        "M1 - Authoritative Storage Core",
        "dist/global/",
    ),
}


def run_documentation_checks() -> list[str]:
    """Validate local Markdown links and the M0 vocabulary dependencies."""

    root = repository_root()
    for document in root.rglob("*.md"):
        _check_links(root, _contained_document(root, document))

    for relative_path, terms in _REQUIRED_TERMS.items():
        content = (root / relative_path).read_text(encoding="utf-8")
        for term in terms:
            if term not in content:
                raise ContractError(f"required blueprint term missing: {term} in {relative_path}")
    return ["docs:local-links", "docs:canonical-terms"]


def _check_links(root: Path, document: Path) -> None:
    content = document.read_text(encoding="utf-8")
    for raw_target in _MARKDOWN_LINK.findall(content):
        target = raw_target.strip().strip("<>")
        if not target or target.startswith("#") or _EXTERNAL_LINK.match(target):
            continue
        path_part = target.split("#", 1)[0]
        if not path_part or Path(path_part).is_absolute():
            continue
        candidate = (document.parent / path_part).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ContractError(f"documentation link escapes repository: {document}: {target}") from error
        if not candidate.exists():
            raise ContractError(f"broken documentation link: {document.relative_to(root)} -> {target}")


def _contained_document(root: Path, document: Path) -> Path:
    resolved = document.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ContractError(f"documentation file escapes repository: {document}") from error
    return resolved


def main() -> int:
    try:
        for check in run_documentation_checks():
            print(f"ok: {check}")
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
