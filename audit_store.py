"""C6-B3 M2: development-only durable audit/correction store (SQLite).

DEV-ONLY OPT-IN. This module exists so that the durability contract
(atomic correction + audit commit, immutable original AI output reference,
owner isolation, optimistic revisions, scoped idempotency, ordered streams)
can be exercised offline, with real transactional semantics, before any
production storage decision is made. It is NOT school-ready durability: one
local file, one host, no tenant model, no retention policy, no replication,
no backups beyond the explicit snapshot API, and no production support.

Why SQLite and not a JSONL append log: the milestone requires ONE atomic
commit that either writes a correction and its audit event together or writes
neither, plus uniqueness enforced IN STORAGE (stream sequence, scoped
idempotency key) and safe concurrent writers. An append-only JSONL log cannot
enforce uniqueness or roll back a partially written record: a crash between
two appends leaves a correction without its audit event, and two writers can
interleave lines. SQLite (stdlib ``sqlite3``) gives real ACID transactions,
constraint-enforced uniqueness and writer serialisation through
``BEGIN IMMEDIATE`` while staying dependency-free and fully offline. The
Postgres migration path is documented in ``AUDIT_STORAGE_CONTRACT.md``.

Security/privacy posture of the stored data:
  - the store holds the owner reference (an opaque subject string, never a
    token), the immutable ORIGINAL AI output DIGEST and a marks-only
    projection (question number -> awarded marks). Raw student answers,
    working summaries, notes and transcripts are NEVER written here;
  - authorization evidence is re-whitelisted at the store boundary to the
    same five short fields the app uses, so a caller that bypasses the app
    layer still cannot widen what is persisted (defence in depth);
  - there is deliberately NO delete/trim/purge API: audit history cannot be
    erased silently. Export exists for authorized data-subject export.

Conventions every caller must respect:
  - every read and write is owner-scoped: a different owner never sees,
    overwrites or extends another owner's stream (cross-owner access fails
    closed as ``result_not_registered`` and leaks nothing);
  - a result must be registered (``register_result``) before any commit, so
    the immutable original output reference always exists first;
  - a commit names the revision it was based on (``expected_revision``): a
    stale write fails with ``stale_revision`` instead of overwriting a
    concurrent decision;
  - an idempotency key is scoped to one owner+result stream: the same key
    with the same payload returns the prior result (retry-safe), the same
    key with a DIFFERENT payload is rejected
    (``idempotency_key_payload_mismatch``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Mapping

__all__ = [
    "MODE_LABEL",
    "SCHEMA_VERSION",
    "EVIDENCE_FIELDS",
    "AUTHORIZATION_SCOPES",
    "AuditStoreError",
    "AuditStore",
    "open_store",
    "restore_from",
    "canonical_json",
    "digest_value",
    "payload_digest",
    "make_idempotency_key",
    "original_score_projection",
    "sanitize_evidence",
]

# Human-readable posture of this adapter; asserted by tests and stored in the
# file itself so a reviewer cannot mistake it for a production durability
# claim.
MODE_LABEL = ("development-only local durability (SQLite, single host, "
              "owner-scoped; NOT school-ready)")

SCHEMA_VERSION = 1

# Independent, store-level re-whitelist of the audit-only authorization
# evidence (mirrors question_review.AUTHORIZATION_EVIDENCE_FIELDS; a caller
# cannot widen it from here either).
EVIDENCE_FIELDS: tuple[str, ...] = (
    "scope", "actor", "authorization_revision", "row_revision", "verified_at",
)
AUTHORIZATION_SCOPES: tuple[str, ...] = ("classroom", "manual", "unscoped")
EVIDENCE_FIELD_LIMIT = 200

MAX_TEXT_LIMIT = 2000
SCORE_LIMIT = 64
IDENTITY_LIMIT = 512

# Named failure-injection stages honoured by ``AuditStore.set_fault_hook``.
FAULT_STAGES: tuple[str, ...] = (
    "begin",                # before any row is written
    "after_event_insert",   # audit event written, correction not yet updated
    "before_commit",        # both rows written, transaction not yet committed
)

_EVENT_SELECT = (
    "SELECT sequence, event_id, idempotency_key, payload_digest, event_type, "
    "question_number, action, review_status, old_score, new_score, reason, "
    "expected_revision, resulting_revision, supersedes_event_id, actor, "
    "binding, authorization_json, created_at FROM audit_events "
    "WHERE owner = ? AND result_id = ? ORDER BY sequence")

_SCHEMA_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    owner                   TEXT    NOT NULL,
    result_id               TEXT    NOT NULL,
    result_revision         INTEGER NOT NULL,
    original_digest         TEXT    NOT NULL,
    original_scores         TEXT    NOT NULL,
    original_scores_digest  TEXT    NOT NULL,
    created_at              TEXT    NOT NULL,
    PRIMARY KEY (owner, result_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
    owner                 TEXT    NOT NULL,
    result_id             TEXT    NOT NULL,
    sequence              INTEGER NOT NULL,
    event_id              TEXT    NOT NULL PRIMARY KEY,
    idempotency_key       TEXT    NOT NULL,
    payload_digest        TEXT    NOT NULL,
    event_type            TEXT    NOT NULL,
    question_number       TEXT    NOT NULL,
    action                TEXT    NOT NULL,
    review_status         TEXT    NOT NULL,
    old_score             TEXT    NOT NULL DEFAULT '',
    new_score             TEXT    NOT NULL DEFAULT '',
    reason                TEXT    NOT NULL DEFAULT '',
    expected_revision     INTEGER NOT NULL,
    resulting_revision    INTEGER NOT NULL,
    supersedes_event_id   TEXT,
    actor                 TEXT    NOT NULL,
    binding               TEXT    NOT NULL DEFAULT '',
    authorization_json    TEXT    NOT NULL DEFAULT '{}',
    created_at            TEXT    NOT NULL,
    UNIQUE (owner, result_id, sequence),
    UNIQUE (owner, result_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS audit_events_stream_question
    ON audit_events (owner, result_id, question_number, sequence);
"""


class AuditStoreError(Exception):
    """Fail-closed storage error; ``code`` is the stable machine-readable reason."""

    def __init__(self, code: str, message: str = ""):
        self.code = str(code)
        super().__init__(message or self.code)


def canonical_json(value: Any) -> str:
    """Canonical, byte-stable JSON used for every digest in this module."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def digest_value(value: Any) -> str:
    """SHA-256 (hex) of the canonical JSON form of ``value``."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def payload_digest(payload: Any) -> str:
    """Digest of the semantic payload a commit is idempotent against."""

    if not isinstance(payload, Mapping):
        raise AuditStoreError("payload_invalid",
                              "correction payload must be a mapping")
    return digest_value(payload)


def make_idempotency_key(*, owner: Any, result_id: Any, payload: Any) -> str:
    """Deterministic retry key for one logical correction operation.

    Derived from owner + result stream + the semantic payload, so a genuine
    retry of the SAME decision maps to the SAME key (and returns the prior
    result), while a different correction is a different operation with a
    different key. Callers that manage their own keys are still bound by the
    storage rule: same key + different payload is rejected.
    """

    if not isinstance(payload, Mapping):
        raise AuditStoreError("payload_invalid",
                              "correction payload must be a mapping")
    return digest_value({"owner": str(owner), "result_id": str(result_id),
                         "payload": payload})


def original_score_projection(original_output: Any) -> list[list[Any]]:
    """Marks-only projection of a pristine AI output (never the answers).

    Returned as an ordered list of ``[question_number, awarded_marks]`` pairs
    so duplicate question numbers stay visible instead of silently collapsing
    into one dict key. No answer text, working summary or note is included.
    """

    if not isinstance(original_output, Mapping):
        return []
    questions = original_output.get("questions", [])
    if not isinstance(questions, (list, tuple)):
        return []
    pairs: list[list[Any]] = []
    for item in questions:
        if isinstance(item, Mapping):
            pairs.append([str(item.get("question_number", "")),
                          item.get("awarded_marks")])
    return pairs


def sanitize_evidence(value: Any) -> dict[str, str]:
    """Store-level re-whitelist of audit-only authorization evidence.

    Only the five known short string fields survive, each bounded; an unknown
    key (token, credential, oversized blob) is dropped, and a non-mapping is
    rejected outright. This duplicates the app-layer whitelist on purpose: a
    caller that reaches the store directly still cannot widen the record.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AuditStoreError("evidence_invalid",
                              "authorization evidence must be a mapping")
    scope = value.get("scope")
    cleaned = {
        "scope": (str(scope) if isinstance(scope, str)
                  and scope in AUTHORIZATION_SCOPES else "unscoped"),
    }
    for field in EVIDENCE_FIELDS:
        if field == "scope":
            continue
        raw = value.get(field)
        cleaned[field] = "" if raw is None else str(raw)[:EVIDENCE_FIELD_LIMIT]
    return cleaned


def _bounded_text(value: Any, name: str, limit: int = MAX_TEXT_LIMIT,
                  allow_empty: bool = True) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    if not allow_empty and not text.strip():
        raise AuditStoreError(f"{name}_invalid", f"{name} must not be empty")
    if len(text) > limit:
        raise AuditStoreError(f"{name}_too_long", f"{name} exceeds {limit}")
    return text


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuditStoreError(f"{name}_invalid", f"{name} must be a non-empty string")
    if len(value) > IDENTITY_LIMIT:
        raise AuditStoreError(f"{name}_too_long", f"{name} exceeds {IDENTITY_LIMIT}")
    return value


def _file_digest(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class AuditStore:
    """Owner-scoped, transactional audit stream store backed by SQLite.

    Not thread-safe by itself: all entry points take an instance lock, and
    the SQLite connection runs in autocommit mode so each mutation issues an
    explicit ``BEGIN IMMEDIATE`` transaction (writer serialisation, real
    rollback on failure).
    """

    def __init__(self, path: str, connection: sqlite3.Connection):
        self._path = str(path)
        self._conn = connection
        self._lock = threading.RLock()
        self._fault: Callable[[str], Any] | None = None
        self._closed = False

    # -- lifecycle -----------------------------------------------------
    @property
    def path(self) -> str:
        return self._path

    @property
    def mode_label(self) -> str:
        return MODE_LABEL

    @property
    def schema_version(self) -> int:
        with self._lock:
            if self._closed:
                raise AuditStoreError("store_closed", "store is closed")
            row = self._conn.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()
        return int(row[0]) if row else 0

    def set_fault_hook(self, hook: Callable[[str], Any] | None) -> None:
        """Failure injection for tests: ``hook(stage)`` may raise to abort a commit."""

        if hook is not None and not callable(hook):
            raise AuditStoreError("fault_hook_invalid", "hook must be callable")
        with self._lock:
            self._fault = hook

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self) -> "AuditStore":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self.close()
        return False

    # -- internal helpers ----------------------------------------------
    def _fault_point(self, stage: str) -> None:
        hook = self._fault
        if hook is not None:
            hook(stage)

    @contextmanager
    def _transaction(self):
        self._lock.acquire()
        try:
            if self._closed:
                raise AuditStoreError("store_closed", "store is closed")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._fault_point("begin")
                yield
                self._fault_point("before_commit")
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            self._lock.release()

    @staticmethod
    def _constraint_error(error: sqlite3.Error) -> AuditStoreError:
        text = str(error).lower()
        if "idempotency" in text:
            return AuditStoreError(
                "idempotency_key_conflict",
                "idempotency key already used in this stream")
        if ".sequence" in text:
            return AuditStoreError(
                "stream_sequence_conflict",
                "stream sequence already used in this stream")
        return AuditStoreError("storage_constraint_violation", str(error))

    def _select_result(self, owner: str, result_id: str):
        return self._conn.execute(
            "SELECT result_revision, original_digest, original_scores, "
            "original_scores_digest, created_at FROM results "
            "WHERE owner = ? AND result_id = ?", (owner, result_id)).fetchone()

    def _require_result(self, owner: str, result_id: str):
        row = self._select_result(owner, result_id)
        if row is None:
            # Owner-scoped miss is indistinguishable from "does not exist":
            # another owner's stream is never visible, here or anywhere else.
            raise AuditStoreError("result_not_registered",
                                  "no result registered for this owner")
        return row

    def _next_sequence(self, owner: str, result_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM audit_events "
            "WHERE owner = ? AND result_id = ?", (owner, result_id)).fetchone()
        return int(row[0]) + 1

    def _select_event_by_key(self, owner: str, result_id: str, key: str):
        return self._conn.execute(
            "SELECT sequence, event_id, payload_digest, resulting_revision "
            "FROM audit_events WHERE owner = ? AND result_id = ? "
            "AND idempotency_key = ?", (owner, result_id, key)).fetchone()

    def _latest_event_for_question(self, owner: str, result_id: str,
                                   question_number: str):
        return self._conn.execute(
            "SELECT event_id FROM audit_events WHERE owner = ? AND result_id = ? "
            "AND question_number = ? ORDER BY sequence DESC LIMIT 1",
            (owner, result_id, question_number)).fetchone()

    # -- results (immutable original output reference) ------------------
    def register_result(self, *, owner: Any, result_id: Any,
                        original_output: Any) -> dict[str, Any]:
        """Idempotently register the immutable original AI output reference.

        Only the SHA-256 digest of the canonical original output and a
        marks-only projection are stored (never answers or working text).
        Re-registering the SAME output returns the existing registration;
        re-registering a DIFFERENT output for the same owner+result is
        refused (``original_output_immutable``) instead of silently
        replacing the original the decisions were reviewed against.
        """

        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        if not isinstance(original_output, Mapping):
            raise AuditStoreError("original_output_invalid",
                                  "original output must be a mapping")
        digest = digest_value(original_output)
        scores = original_score_projection(original_output)
        scores_json = canonical_json(scores)
        scores_digest = digest_value(scores)
        with self._transaction():
            row = self._select_result(owner, result_id)
            if row is not None:
                if row[1] != digest:
                    raise AuditStoreError(
                        "original_output_immutable",
                        "a different original output is already registered")
                return {"created": False, "owner": owner, "result_id": result_id,
                        "result_revision": int(row[0]), "original_digest": digest,
                        "original_scores": scores}
            created_at = f"{time.time():.6f}"
            try:
                self._conn.execute(
                    "INSERT INTO results (owner, result_id, result_revision, "
                    "original_digest, original_scores, original_scores_digest, "
                    "created_at) VALUES (?, ?, 0, ?, ?, ?, ?)",
                    (owner, result_id, digest, scores_json, scores_digest,
                     created_at))
            except sqlite3.IntegrityError as error:
                # A concurrent writer may have registered the same stream
                # first: identical output is an idempotent success, anything
                # different still fails closed below.
                raced = self._select_result(owner, result_id)
                if raced is not None and raced[1] == digest:
                    return {"created": False, "owner": owner,
                            "result_id": result_id,
                            "result_revision": int(raced[0]),
                            "original_digest": digest,
                            "original_scores": scores}
                raise self._constraint_error(error) from error
            return {"created": True, "owner": owner, "result_id": result_id,
                    "result_revision": 0, "original_digest": digest,
                    "original_scores": scores}

    def current_revision(self, *, owner: Any, result_id: Any) -> int | None:
        """Optimistic-concurrency revision of a stream, or None if unregistered."""

        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        with self._lock:
            if self._closed:
                raise AuditStoreError("store_closed", "store is closed")
            row = self._select_result(owner, result_id)
        return None if row is None else int(row[0])

    def original_record(self, *, owner: Any, result_id: Any) -> dict[str, Any]:
        """Owner-scoped immutable original reference (digest + marks projection)."""

        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        with self._lock:
            if self._closed:
                raise AuditStoreError("store_closed", "store is closed")
            row = self._require_result(owner, result_id)
        scores = json.loads(row[2])
        return {"owner": owner, "result_id": result_id,
                "result_revision": int(row[0]), "original_digest": row[1],
                "original_scores": scores, "created_at": row[4]}

    # -- corrections (atomic with their audit event) --------------------
    def commit_correction(self, *, owner: Any, result_id: Any,
                          expected_revision: Any, idempotency_key: Any,
                          payload: Any, event: Any) -> dict[str, Any]:
        """Atomically append one audit event and advance the stream revision.

        Either the event lands and the result revision moves, or nothing is
        written (a fault, constraint violation or stale revision rolls the
        whole transaction back — a correction never exists without its audit
        event). Returns ``{"status": "committed" | "duplicate", ...}``; a
        duplicate retry returns the PRIOR result (same sequence and event id)
        without appending a second event.
        """

        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        key = _identity(idempotency_key, "idempotency_key")
        if not isinstance(payload, Mapping):
            raise AuditStoreError("payload_invalid",
                                  "correction payload must be a mapping")
        if not isinstance(event, Mapping):
            raise AuditStoreError("event_invalid", "audit event must be a mapping")
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError) as error:
            raise AuditStoreError("revision_invalid",
                                  "expected_revision must be an integer") from error
        if expected < 0:
            raise AuditStoreError("revision_invalid",
                                  "expected_revision must not be negative")
        digest = payload_digest(payload)
        fields = _event_fields(event)
        with self._transaction():
            row = self._require_result(owner, result_id)
            prior = self._select_event_by_key(owner, result_id, key)
            if prior is not None:
                if prior[2] != digest:
                    raise AuditStoreError(
                        "idempotency_key_payload_mismatch",
                        "idempotency key reused with a different payload")
                return {"status": "duplicate", "owner": owner,
                        "result_id": result_id, "sequence": int(prior[0]),
                        "event_id": prior[1], "idempotency_key": key,
                        "resulting_revision": int(prior[3]),
                        "idempotent": True}
            if expected != int(row[0]):
                raise AuditStoreError(
                    "stale_revision",
                    f"expected revision {expected}, store holds {int(row[0])}")
            previous = self._latest_event_for_question(
                owner, result_id, fields["question_number"])
            sequence = self._next_sequence(owner, result_id)
            resulting = expected + 1
            event_id = uuid.uuid4().hex
            created_at = f"{time.time():.6f}"
            try:
                self._conn.execute(
                    "INSERT INTO audit_events (owner, result_id, sequence, "
                    "event_id, idempotency_key, payload_digest, event_type, "
                    "question_number, action, review_status, old_score, "
                    "new_score, reason, expected_revision, resulting_revision, "
                    "supersedes_event_id, actor, binding, authorization_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?)",
                    (owner, result_id, sequence, event_id, key, digest,
                     fields["event_type"], fields["question_number"],
                     fields["action"], fields["review_status"],
                     fields["old_score"], fields["new_score"], fields["reason"],
                     expected, resulting,
                     previous[0] if previous is not None else None,
                     fields["actor"], fields["binding"],
                     canonical_json(fields["authorization_evidence"]),
                     created_at))
            except sqlite3.IntegrityError as error:
                raise self._constraint_error(error) from error
            self._fault_point("after_event_insert")
            try:
                self._conn.execute(
                    "UPDATE results SET result_revision = ? "
                    "WHERE owner = ? AND result_id = ?",
                    (resulting, owner, result_id))
            except sqlite3.IntegrityError as error:
                raise self._constraint_error(error) from error
            return {"status": "committed", "owner": owner, "result_id": result_id,
                    "sequence": sequence, "event_id": event_id,
                    "idempotency_key": key,
                    "resulting_revision": resulting, "idempotent": False}

    # -- reads, replay, verification, export ----------------------------
    def events(self, *, owner: Any, result_id: Any,
               limit: int | None = None) -> list[dict[str, Any]]:
        """Ordered audit events of one owner-scoped stream (oldest first)."""

        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        with self._lock:
            if self._closed:
                raise AuditStoreError("store_closed", "store is closed")
            self._require_result(owner, result_id)
            sql = _EVENT_SELECT
            params: tuple[Any, ...] = (owner, result_id)
            if limit is not None:
                sql += " LIMIT ?"
                params = params + (int(limit),)
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(row) for row in rows]

    def _stream_snapshot(self, owner: str, result_id: str, *,
                         tolerate_bad_evidence: bool = False):
        """Consistent ``(result row, events, evidence error)`` read.

        Both reads happen inside ONE read transaction, so the revision row and
        its events are always observed at the same point in time: replay must
        never report a false break just because a concurrent writer committed
        between two separate SELECTs. With ``tolerate_bad_evidence`` the first
        record that fails the evidence re-whitelist is returned as an error
        (``verify`` reports it) instead of raised (``replay`` raises it).
        """
        with self._lock:
            if self._closed:
                raise AuditStoreError("store_closed", "store is closed")
            self._conn.execute("BEGIN")
            try:
                row = self._select_result(owner, result_id)
                raw = self._conn.execute(_EVENT_SELECT,
                                         (owner, result_id)).fetchall()
                events: list[dict[str, Any]] = []
                evidence_error: AuditStoreError | None = None
                for record in raw:
                    try:
                        events.append(_row_to_event(record))
                    except AuditStoreError as error:
                        if not tolerate_bad_evidence:
                            raise
                        evidence_error = error
                        events = []
                        break
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return row, events, evidence_error

    def replay(self, *, owner: Any, result_id: Any) -> list[dict[str, Any]]:
        """Ordered replay that fails closed on any break in the stream."""

        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        row, rows, _ = self._stream_snapshot(owner, result_id)
        if row is None:
            # Owner-scoped miss: another owner's stream is never visible here.
            raise AuditStoreError("result_not_registered",
                                  "no result registered for this owner")
        problems = _chain_problems(rows, result_revision=int(row[0]))
        if problems:
            raise AuditStoreError("audit_order_invalid", "; ".join(problems))
        return rows

    def verify(self, *, owner: Any, result_id: Any) -> dict[str, Any]:
        """Integrity report for one stream: DB check, order chain, projections.

        Never trusts stored bytes at face value: re-runs SQLite's own
        integrity check, revalidates the event chain (sequence, revisions,
        uniqueness, supersedes references, evidence whitelist) and recomputes
        the stored marks-projection digest. Any deviation is reported in
        ``problems`` with ``ok`` False (fail closed for any caller).
        """

        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        problems: list[str] = []
        with self._lock:
            if self._closed:
                raise AuditStoreError("store_closed", "store is closed")
            integrity_rows = self._conn.execute(
                "PRAGMA integrity_check").fetchall()
            if not integrity_rows or str(integrity_rows[0][0]).lower() != "ok":
                problems.append(
                    "sqlite_integrity_check:" + ",".join(
                        str(row[0]) for row in integrity_rows))
            row, events, evidence_error = self._stream_snapshot(
                owner, result_id, tolerate_bad_evidence=True)
            if row is None:
                problems.append("result_not_registered")
            else:
                try:
                    stored = json.loads(row[2])
                except (TypeError, ValueError):
                    stored = None
                    problems.append("original_scores_unparseable")
                if stored is not None and digest_value(stored) != row[3]:
                    problems.append("original_projection_digest_mismatch")
                if evidence_error is not None:
                    # An unreadable/tampered record is reported, not raised:
                    # verification's job is to describe the damage.
                    problems.append(
                        f"audit_events_unreadable:{evidence_error.code}")
                else:
                    problems.extend(_chain_problems(
                        events, result_revision=int(row[0])))
        return {"ok": not problems, "problems": problems,
                "mode": MODE_LABEL, "schema_version": self.schema_version,
                "owner": owner, "result_id": result_id,
                "event_count": len(events),
                "sequences": [event["sequence"] for event in events],
                "original_digest": row[1] if row is not None else "",
                "integrity_check": "ok" if not any(
                    problem.startswith("sqlite_integrity_check")
                    for problem in problems) else "failed"}

    def export_stream(self, *, owner: Any, result_id: Any) -> dict[str, Any]:
        """Authorized export of one owner-scoped stream (marks + events only)."""

        record = self.original_record(owner=owner, result_id=result_id)
        events = self.replay(owner=owner, result_id=result_id)
        return {"mode": MODE_LABEL, "schema_version": self.schema_version,
                "result": record, "events": events}

    # -- snapshot / restore --------------------------------------------
    def snapshot_to(self, path: str) -> dict[str, Any]:
        """Consistent online snapshot of the whole store (SQLite backup API)."""

        target = str(path)
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        destination = sqlite3.connect(target)
        try:
            with self._lock:
                if self._closed:
                    raise AuditStoreError("store_closed", "store is closed")
                self._conn.backup(destination)
        finally:
            destination.close()
        _validate_snapshot(target)
        return {"path": target, "sha256": _file_digest(target),
                "mode": MODE_LABEL, "schema_version": SCHEMA_VERSION}


def _row_to_event(row: Any) -> dict[str, Any]:
    """One audit_events row as a plain mapping (evidence re-whitelisted)."""

    (sequence, event_id, key, digest, event_type, question_number, action,
     review_status, old_score, new_score, reason, expected, resulting,
     supersedes, actor, binding, authorization_json, created_at) = row
    try:
        stored_evidence = json.loads(authorization_json)
    except (TypeError, ValueError) as error:
        raise AuditStoreError("audit_record_invalid",
                              "authorization evidence is not valid JSON") from error
    if (not isinstance(stored_evidence, Mapping)
            or set(stored_evidence) != set(EVIDENCE_FIELDS)):
        # A record whose evidence fields were widened (by tampering or a bug)
        # is refused, never silently repaired: it is not the record that was
        # committed.
        raise AuditStoreError("audit_record_invalid",
                              "authorization evidence does not match the "
                              "whitelisted fields")
    evidence = sanitize_evidence(stored_evidence)
    return {"sequence": int(sequence), "event_id": event_id,
            "idempotency_key": key, "payload_digest": digest,
            "event_type": event_type, "question_number": question_number,
            "action": action, "review_status": review_status,
            "old_score": old_score, "new_score": new_score, "reason": reason,
            "expected_revision": int(expected),
            "resulting_revision": int(resulting),
            "supersedes_event_id": supersedes, "actor": actor,
            "binding": binding, "authorization_evidence": evidence,
            "created_at": created_at}


def _event_fields(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise the caller-supplied part of one audit event."""

    evidence = sanitize_evidence(event.get("authorization_evidence"))
    return {
        "event_type": _bounded_text(event.get("event_type") or "question_review_decision",
                                    "event_type", 64, allow_empty=False),
        "question_number": _bounded_text(event.get("question_number"),
                                         "question_number", SCORE_LIMIT,
                                         allow_empty=False),
        "action": _bounded_text(event.get("action"), "action", 64),
        "review_status": _bounded_text(event.get("review_status"),
                                       "review_status", 64),
        "old_score": _bounded_text(event.get("old_score"), "old_score", SCORE_LIMIT),
        "new_score": _bounded_text(event.get("new_score"), "new_score", SCORE_LIMIT),
        "reason": _bounded_text(event.get("reason"), "reason"),
        "actor": _bounded_text(event.get("actor"), "actor",
                               EVIDENCE_FIELD_LIMIT, allow_empty=False),
        "binding": _bounded_text(event.get("binding"), "binding",
                                 IDENTITY_LIMIT),
        "authorization_evidence": evidence,
    }


def _chain_problems(events: list[dict[str, Any]],
                    result_revision: int | None = None) -> list[str]:
    """Structural problems in one replayed stream (empty when sound).

    When ``result_revision`` is given, the chain's last resulting revision
    must equal it, so a revision row that no committed event explains (a
    silent UPDATE) is detected as well.
    """

    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    previous_resulting: int | None = None
    for index, event in enumerate(events, start=1):
        if event["sequence"] != index:
            problems.append(
                f"sequence_gap:expected_{index}_got_{event['sequence']}")
        if previous_resulting is not None and \
                event["expected_revision"] != previous_resulting:
            problems.append(f"revision_chain_break_at_{index}")
        previous_resulting = event["resulting_revision"]
        if event["event_id"] in seen_ids:
            problems.append(f"duplicate_event_id_at_{index}")
        seen_ids.add(event["event_id"])
        if event["idempotency_key"] in seen_keys:
            problems.append(f"duplicate_idempotency_key_at_{index}")
        seen_keys.add(event["idempotency_key"])
        if event["supersedes_event_id"] is not None and \
                event["supersedes_event_id"] not in seen_ids:
            problems.append(f"supersedes_reference_missing_at_{index}")
        if set(event["authorization_evidence"]) != set(EVIDENCE_FIELDS):
            problems.append(f"evidence_fields_widened_at_{index}")
    if result_revision is not None:
        chain_head = previous_resulting if previous_resulting is not None else 0
        if int(result_revision) != chain_head:
            problems.append(
                f"result_revision_mismatch:row_{int(result_revision)}"
                f"_chain_{chain_head}")
    return problems


def _validate_snapshot(path: str) -> None:
    """Refuse to call a file a valid store snapshot unless it verifies."""

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise AuditStoreError("snapshot_invalid", str(error)) from error
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        if not rows or str(rows[0][0]).lower() != "ok":
            raise AuditStoreError("snapshot_invalid",
                                  "snapshot fails the SQLite integrity check")
        version = connection.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()
        if version is None or int(version[0]) != SCHEMA_VERSION:
            raise AuditStoreError("snapshot_unsupported",
                                  "snapshot schema version is not supported")
    except sqlite3.Error as error:
        raise AuditStoreError("snapshot_invalid", str(error)) from error
    finally:
        connection.close()


def open_store(path: str, *, dev_only_ack: bool = False) -> AuditStore:
    """Open (creating if needed) the development-only audit store.

    Requires the explicit ``dev_only_ack=True`` opt-in: persistence never
    happens by accident, and the file itself records its development-only
    mode. Fails closed on an unusable file or an unsupported schema version
    (no silent migration in a development adapter).
    """

    if dev_only_ack is not True:
        raise AuditStoreError("dev_opt_in_required",
                              "development-only persistence must be opted into "
                              "explicitly")
    target = str(path)
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = None
    try:
        connection = sqlite3.connect(target, timeout=30.0,
                                     isolation_level=None,
                                     check_same_thread=False)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.executescript(_SCHEMA_TABLES_SQL)
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise AuditStoreError("store_unusable", str(error)) from error
    store = AuditStore(target, connection)
    try:
        with store._lock:
            row = connection.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                connection.execute(
                    "INSERT OR IGNORE INTO store_meta (key, value) VALUES "
                    "('schema_version', ?), ('mode', ?)",
                    (str(SCHEMA_VERSION), "development-only"))
            elif int(row[0]) != SCHEMA_VERSION:
                store.close()
                raise AuditStoreError(
                    "schema_version_unsupported",
                    f"store schema version {row[0]} is not supported")
    except sqlite3.Error as error:
        store.close()
        raise AuditStoreError("store_unusable", str(error)) from error
    return store


def restore_from(snapshot_path: str, target_path: str, *,
                 dev_only_ack: bool = False) -> dict[str, Any]:
    """Restore a verified snapshot over a store file (atomic replace).

    Every open store on the target must be closed first. The snapshot is
    verified (integrity check + schema version) BEFORE it replaces anything,
    stale WAL sidecars of the old database are removed so they can never be
    replayed onto the new file, and the replacement itself is a single
    ``os.replace``.
    """

    if dev_only_ack is not True:
        raise AuditStoreError("dev_opt_in_required",
                              "development-only persistence must be opted into "
                              "explicitly")
    snapshot = str(snapshot_path)
    target = str(target_path)
    if not os.path.isfile(snapshot):
        raise AuditStoreError("snapshot_missing", "snapshot file does not exist")
    _validate_snapshot(snapshot)
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    staging = target + ".restore-tmp"
    shutil.copyfile(snapshot, staging)
    # Durability of the staged copy before it replaces anything (opened
    # read/write: a read-only descriptor cannot be fsynced on Windows).
    with open(staging, "r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    for suffix in ("-wal", "-shm"):
        stale = target + suffix
        if os.path.exists(stale):
            os.remove(stale)
    os.replace(staging, target)
    return {"restored": True, "path": target, "snapshot": snapshot,
            "snapshot_sha256": _file_digest(snapshot), "mode": MODE_LABEL}
