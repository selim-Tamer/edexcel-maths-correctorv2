"""Private Supabase Storage adapter for document bytes (production).

The development store (``document_store.py``) keeps PDF bytes in SQLite. The
production composition keeps them in a **private** Supabase Storage bucket and
records only metadata in PostgreSQL's ``pdf_references``: server-derived
sha256 + object key + size. No bytes, no filenames, no pupil names and no
public or presigned URLs are stored or returned anywhere - the API serves the
bytes itself through the owner/tenant-scoped read route.

Interface mirrors ``document_store`` exactly (``require_connection``,
``put_document``, ``find_document``, ``close_quietly``) so the API's document
wiring is one facade. Two verbs are production-specific:

* ``delete_document`` is the RETENTION/PURGE verb and must run as the separate
  elevated role (``marker_purge``, schema 0004). Runtime roles hold zero DELETE.
* object keys are derived server-side (``storage_key``), never client-supplied.

Security posture:
* the service key stays server-side only: it is read from the runtime contract
  and never logged, echoed, or placed in a URL;
* content is bounded by the application's 25 MiB cap (defense in depth - the
  API validates through ``document_limits`` first);
* the tenant binding is transaction-local and taken from the caller's verified
  identity, never from a request field;
* a failed metadata write after a successful upload is fail-closed: the
  orphaned object is deleted on a best-effort basis and the call reports
  ``store_unusable`` rather than returning success. The ONE exception is the
  benign concurrent-upload race (IR finding): if the row that rejected our
  INSERT now exists with our size, another upload of identical bytes won and
  its row references the very object we uploaded - we report ``False`` and
  never delete it.

NOTHING here is deployed or provisioned: the bucket, its service key and the
operator roles are created by the reviewed setup packet, not by this code.
"""
from __future__ import annotations

import hashlib
import os
from typing import Callable, Optional

import psycopg

from document_store import DocumentStoreError
from runtime_config import (BUCKET_ENV, PG_DSN_ENV, SUPABASE_KEY_ENV,
                            SUPABASE_URL_ENV)

TENANT_GUC = "app.tenant_id"

# The application cap (document_limits / MARKER_MAX_DOCUMENT_MIB is 25 MiB).
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

_DOCUMENT_MEDIA_TYPE = "application/pdf"


def configured() -> bool:
    """True when this process is composed with the private bucket."""
    return all((os.environ.get(name) or "").strip()
               for name in (SUPABASE_URL_ENV, SUPABASE_KEY_ENV, BUCKET_ENV))


def storage_key(tenant_id: str, owner: str, document_digest: str) -> str:
    """Server-derived object key: deterministic, no PII, no client influence.

    The key is a hash of (tenant, owner, digest), so it carries no subject or
    school name into object storage, and a client cannot aim a lookup at
    anyone else's object by choosing a key.
    """
    material = f"{tenant_id}\n{owner}\n{document_digest}".encode("utf-8")
    return ("objects/" + hashlib.sha256(material).hexdigest()[:40]
            + "/" + document_digest + ".pdf")


class SupabaseDocumentStore:
    """One private-bucket handle: metadata rows + objects, tenant-bound."""

    def __init__(self, *, url: str, service_key: str, bucket: str,
                 connection, transport: Optional[Callable] = None):
        self._url = url.rstrip("/")
        self._key = service_key
        self._bucket = bucket
        self._conn = connection
        self._transport = transport
        self._closed = False

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, key: str, body: bytes = None) -> tuple:
        """One storage REST call. Returns (status, content).

        The service key is placed in the Authorization header by this method
        and by nothing else; it is never part of a URL or a log line.
        """
        url = (f"{self._url}/storage/v1/object/"
               f"{self._bucket}/{key}")
        headers = {"Authorization": f"Bearer {self._key}"}
        if body is not None:
            headers["Content-Type"] = _DOCUMENT_MEDIA_TYPE
            headers["x-upsert"] = "true"
        if self._transport is not None:
            return self._transport(method, url, headers, body)
        try:
            import requests  # lazy: only the production adapter needs it
            response = requests.request(method, url, headers=headers,
                                        data=body, timeout=60)
            return response.status_code, response.content
        except Exception as error:
            raise DocumentStoreError("store_unusable") from error

    def _bind(self, tenant_id: str) -> None:
        self._conn.execute("SELECT set_config(%s, %s, true)",
                           (TENANT_GUC, tenant_id))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._conn.close()
            except Exception:
                pass


def require_connection(*, transport: Optional[Callable] = None):
    """Open the private store from env, failing closed when unconfigured."""
    if not configured():
        raise DocumentStoreError("dev_opt_in_required")
    url = (os.environ.get(SUPABASE_URL_ENV) or "").strip()
    service_key = (os.environ.get(SUPABASE_KEY_ENV) or "").strip()
    bucket = (os.environ.get(BUCKET_ENV) or "").strip()
    try:
        connection = psycopg.connect(
            (os.environ.get(PG_DSN_ENV) or "").strip(),
            prepare_threshold=None)
    except Exception as error:
        raise DocumentStoreError("store_unusable") from error
    try:
        with connection.transaction():
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'docs_version'"
            ).fetchone()
    except Exception as error:
        connection.close()
        raise DocumentStoreError("store_unusable") from error
    if row is None or str(row[0]) != "1":
        connection.close()
        raise DocumentStoreError("store_unusable")
    return SupabaseDocumentStore(url=url, service_key=service_key,
                                 bucket=bucket, connection=connection,
                                 transport=transport)


def _valid_digest(document_digest: str) -> str:
    digest = str(document_digest or "").strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise DocumentStoreError("store_unusable")
    return digest


def put_document(store: SupabaseDocumentStore, *, tenant_id: str, owner: str,
                 document_digest: str, filename: str, page_count,
                 content: bytes) -> bool:
    """Store one document idempotently. Returns True when newly stored.

    A repeat upload of the same content for the same owner+tenant returns
    False without rewriting anything. ``filename`` and ``page_count`` are
    accepted for interface parity with the dev store and are deliberately NOT
    stored: ``pdf_references`` keeps digest + key + size only.

    Two concurrent uploads of identical bytes are also resolved safely: the
    loser of the primary-key race returns False and keeps the winner's object
    (they are the same bytes), instead of deleting it.
    """
    digest = _valid_digest(document_digest)
    payload = bytes(content)
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise DocumentStoreError("document_too_large")
    key = storage_key(tenant_id, owner, digest)
    try:
        with store._conn.transaction():
            store._bind(tenant_id)
            prior = store._conn.execute(
                "SELECT size_bytes FROM pdf_references WHERE tenant_id = %s"
                " AND owner_subject = %s AND document_digest = %s",
                (tenant_id, owner, digest)).fetchone()
            if prior is not None:
                if prior[0] is not None and int(prior[0]) != len(payload):
                    raise DocumentStoreError("store_unusable")
                return False
            status, _ = store._request("PUT", key, payload)
            if status not in (200, 201):
                raise DocumentStoreError("store_unusable")
            try:
                # Savepoint: a failed INSERT must not abort the outer
                # transaction, because the race check below still needs to
                # read.
                with store._conn.transaction():
                    store._conn.execute(
                        "INSERT INTO pdf_references (tenant_id, owner_subject,"
                        " document_digest, storage_key, size_bytes)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        (tenant_id, owner, digest, key, len(payload)))
            except Exception as error:
                # Concurrent identical upload (IR finding): under READ
                # COMMITTED a racer can commit the same (tenant, owner,
                # digest) row after our prior-SELECT and before our INSERT.
                # The PK then rejects ours - and the bytes both uploads wrote
                # are the same object the winner's row references. So: if a
                # row is now there with our size, the race is BENIGN (report
                # not-created) and the object must NOT be deleted. Only a
                # genuinely ownerless upload is an orphan worth removing.
                try:
                    raced = store._conn.execute(
                        "SELECT size_bytes FROM pdf_references"
                        " WHERE tenant_id = %s AND owner_subject = %s"
                        " AND document_digest = %s",
                        (tenant_id, owner, digest)).fetchone()
                except Exception:
                    raced = None
                if raced is not None and (raced[0] is None
                                          or int(raced[0]) == len(payload)):
                    return False
                try:
                    store._request("DELETE", key)
                except Exception:
                    pass
                raise DocumentStoreError("store_unusable") from error
            return True
    except DocumentStoreError:
        raise
    except Exception as error:
        raise DocumentStoreError("store_unusable") from error


def find_document(store: SupabaseDocumentStore, *, tenant_id: str, owner: str,
                  document_digest: str):
    """The stored bytes for this exact owner+tenant, or None (never leaked).

    A tombstoned row (``storage_key = ''``, i.e. purged) is a miss like any
    other: the retention decision is not a distinguishable error.
    """
    digest = _valid_digest(document_digest)
    key = storage_key(tenant_id, owner, digest)
    try:
        with store._conn.transaction():
            store._bind(tenant_id)
            row = store._conn.execute(
                "SELECT storage_key FROM pdf_references WHERE tenant_id = %s"
                " AND owner_subject = %s AND document_digest = %s",
                (tenant_id, owner, digest)).fetchone()
            if row is None or not str(row[0]):
                return None
            status, body = store._request("GET", str(row[0]))
    except DocumentStoreError:
        raise
    except Exception as error:
        raise DocumentStoreError("store_unusable") from error
    if status != 200:
        # Row without object: the store is inconsistent, never a silent miss.
        raise DocumentStoreError("store_unusable")
    return bytes(body)


def delete_document(store: SupabaseDocumentStore, *, tenant_id: str,
                    owner: str, document_digest: str) -> bool:
    """RETENTION/PURGE verb: remove the bytes, keep the digest tombstone.

    The reviewed retention proposal deletes the object and KEEPS the
    ``pdf_references`` row as a tombstone for audit ("deletion removes the
    object, keeps the digest/tombstone row"), so this updates the row to
    ``storage_key = ''`` instead of deleting it - which is why NO role in the
    schema holds any DELETE at all. Idempotent: purging twice is a no-op.

    Must run as the separate elevated role (``marker_purge``, 0004). Tenant-
    bound like everything else: a purge is per tenant with the binding set,
    never a privileged cross-tenant delete. Returns True when this call
    removed the object.
    """
    digest = _valid_digest(document_digest)
    key = storage_key(tenant_id, owner, digest)
    try:
        with store._conn.transaction():
            store._bind(tenant_id)
            row = store._conn.execute(
                "SELECT storage_key FROM pdf_references WHERE tenant_id = %s"
                " AND owner_subject = %s AND document_digest = %s",
                (tenant_id, owner, digest)).fetchone()
            if row is None or not str(row[0]):
                return False
            status, _ = store._request("DELETE", str(row[0]))
            if status not in (200, 204, 404):
                raise DocumentStoreError("store_unusable")
            store._conn.execute(
                "UPDATE pdf_references SET storage_key = '', size_bytes = NULL"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND document_digest = %s",
                (tenant_id, owner, digest))
            return True
    except DocumentStoreError:
        raise
    except Exception as error:
        raise DocumentStoreError("store_unusable") from error


def close_quietly(store) -> None:
    if store is not None:
        try:
            store.close()
        except Exception:
            pass
