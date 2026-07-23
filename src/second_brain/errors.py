"""Stable exception types used by the M0 contract checks."""


class ContractError(ValueError):
    """A document violates a declared Second Brain contract."""


class DuplicateKeyError(ContractError):
    """A JSON object repeats a key and therefore has ambiguous meaning."""


class SchemaValidationError(ContractError):
    """A value does not conform to its machine-readable JSON Schema."""


class SemanticValidationError(ContractError):
    """A value has a valid shape but violates a cross-field invariant."""


class WorkspacePathError(ContractError):
    """A requested path would escape the repository-local workspace."""


class StorageError(ContractError):
    """A stable, redaction-safe error emitted by the authoritative store."""

    def __init__(self, code: str, message: str | None = None, **details: object) -> None:
        self.code = code
        self.details = details
        super().__init__(message or code)


class PathUnsafeError(StorageError):
    """A path, symlink, or filesystem entry is outside the store boundary."""

    def __init__(self, message: str = "unsafe store path") -> None:
        super().__init__("PATH_UNSAFE", message)


class IntegrityError(StorageError):
    """Authoritative state cannot be trusted without manual repair."""

    def __init__(self, message: str = "authoritative integrity check failed") -> None:
        super().__init__("INTEGRITY_FAILED", message)


class StoreDegradedError(StorageError):
    """A write was attempted while the store is intentionally read-only."""

    def __init__(self, message: str = "store is degraded and read-only") -> None:
        super().__init__("STORE_DEGRADED", message)


class RevisionConflictError(StorageError):
    """A CAS precondition no longer matches the authoritative object."""

    def __init__(
        self,
        *,
        object_id: str,
        current_revision: int | None,
        current_content_hash: str | None,
    ) -> None:
        super().__init__(
            "REVISION_CONFLICT",
            "revision or content hash no longer matches",
            object_id=object_id,
            current_revision=current_revision,
            current_content_hash=current_content_hash,
        )
        self.object_id = object_id
        self.current_revision = current_revision
        self.current_content_hash = current_content_hash


class IdempotencyKeyReusedError(StorageError):
    """An idempotency key was already committed for another intent."""

    def __init__(self, message: str = "idempotency key is already bound to another intent") -> None:
        super().__init__("IDEMPOTENCY_KEY_REUSED", message)


class AuthorityDeniedError(StorageError):
    """The caller lacks authority for a requested durable mutation."""

    def __init__(self, message: str = "authoritative mutation is not authorized") -> None:
        super().__init__("AUTHORITY_DENIED", message)


class RelationInvalidError(StorageError):
    """A relation is dangling, cross-store, cyclic, or otherwise unsafe."""

    def __init__(self, message: str = "relation is invalid") -> None:
        super().__init__("RELATION_INVALID", message)


class ContentPolicyError(StorageError):
    """Content was blocked without exposing the matched sensitive text."""

    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        self.reason_codes = reason_codes
        super().__init__(
            "CONTENT_POLICY_REJECTED",
            "content rejected by policy: " + ", ".join(reason_codes),
            reason_codes=reason_codes,
        )


class ParseError(StorageError):
    """A strict JSON, NDJSON, YAML subset, or Markdown parse failed."""

    def __init__(self, message: str = "invalid serialized input") -> None:
        super().__init__("PARSE_INVALID", message)
