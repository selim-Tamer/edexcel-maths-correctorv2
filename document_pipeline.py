"""Session-owned document transport. No global cache, student bytes or credentials on disk.

The only disk copy is a short-lived SDK upload input, removed in a finally block.
Callbacks receive timing data only; callers must marshal UI updates onto their main thread.
"""
import copy
import hashlib
import os
import tempfile
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from threading import RLock, BoundedSemaphore


def digest(data):
    return hashlib.sha256(data).hexdigest()


def state_of(remote):
    state = getattr(remote, "state", None)
    return getattr(state, "name", state)


def error_code(exc):
    value = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def request_config(seconds):
    # Disable hidden SDK retries; all polling retries consume our explicit deadline.
    return {"http_options": {"timeout": max(1, int(seconds * 1000)),
                             "retry_options": {"attempts": 1}}}


def retry_delay(exc, fallback, now):
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is not None:
        try:
            return max(0.0, float(value))
        except (ValueError, TypeError):
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
    return fallback


def get_status(client, name, deadline, clock=time):
    for attempt in range(3):
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise TimeoutError("Gemini file status deadline expired.")
        try:
            remote = client.files.get(name=name, config=request_config(min(30, remaining)))
            if clock.monotonic() > deadline:
                raise TimeoutError("Gemini file status deadline expired.")
            return remote
        except Exception as exc:
            if error_code(exc) not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            delay = retry_delay(exc, 2 ** (attempt + 1), clock.time())
            if delay >= deadline - clock.monotonic():
                raise TimeoutError("Gemini file status retry exceeds the processing deadline.") from exc
            clock.sleep(delay)
    raise AssertionError("Unreachable")


def transfer(client, data, filename, progress=None, clock=time):
    import pipeline_metrics
    with pipeline_metrics.job("document", clock):
        return _transfer(client, data, filename, progress, clock)


def _transfer(client, data, filename, progress=None, clock=time):
    """Upload once, poll with bounded retries, preserve bytes, clean failed uploads.

    Never retry an ambiguous upload POST: it may have created a server-side file.
    Transfer requests are capped at 120 seconds; processing has a separate 180s budget.
    """
    import pipeline_metrics
    import document_limits
    # Final trusted boundary (Stage S3): no document reaches the provider
    # without passing the structural checks, whichever path produced it.
    document_limits.validate_document(
        data, filename, allow_images=True, deep=False)
    def emit(stage, started, **extra):
        if extra.get("done") and stage == "Local preparation":
            pipeline_metrics.record("preparation", clock.monotonic() - started)
        if progress:
            progress({"stage": stage, "elapsed": max(0, clock.monotonic() - started), **extra})

    prepared = clock.monotonic()
    tmp_path = None
    remote = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1] or ".bin") as tmp:
            tmp_path = tmp.name
            tmp.write(data)
        emit("Local preparation", prepared, bytes=len(data), done=True)
        started = clock.monotonic()
        emit("Transfer to Gemini", started, done=False)
        remote = pipeline_metrics.call("upload", client.files.upload, file=tmp_path, config=request_config(120))
        emit("Transfer to Gemini", started, done=True)
        started = clock.monotonic()
        try:
            deadline = started + 180
            interval = 0.5
            polls = 0
            emit("Gemini processing", started, done=False)
            while state_of(remote) == "PROCESSING":
                remaining = deadline - clock.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Gemini file processing timed out after 180 seconds.")
                clock.sleep(min(interval, remaining))
                remote = get_status(client, remote.name, deadline, clock)
                polls += 1
                interval = min(2.0, interval * 2)
                emit("Gemini processing", started, polls=polls, done=False)
            if clock.monotonic() > deadline:
                raise TimeoutError("Gemini file processing timed out after 180 seconds.")
            if state_of(remote) not in {"ACTIVE", "SUCCEEDED"}:
                raise RuntimeError("Gemini could not finish processing the document (failed or unknown file state).")
            emit("Gemini processing", started, polls=polls, done=True)
        finally:
            pipeline_metrics.record("file_processing", clock.monotonic() - started)
        return remote
    except BaseException:
        if remote is not None:
            try:
                client.files.delete(name=remote.name)
            except Exception:
                pass
        raise
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


class Memo:
    """Bounded, non-sliding TTL for session-only parsed schemes and marking results."""
    def __init__(self, ttl=600, capacity=128, clock=time):
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self.entries = {}

    def get(self, key):
        item = self.entries.get(key)
        if item and self.clock.monotonic() < item[0]:
            return copy.deepcopy(item[1])
        self.entries.pop(key, None)
        return None

    def put(self, key, value):
        for old in list(self.entries):
            if self.entries[old][0] <= self.clock.monotonic():
                del self.entries[old]
        if key not in self.entries and len(self.entries) >= self.capacity:
            self.entries.pop(next(iter(self.entries)))
        self.entries[key] = (self.clock.monotonic() + self.ttl, copy.deepcopy(value))


def expiration(remote):
    value = getattr(remote, "expiration_time", None)
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None:
            return None
        return value.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


class FileCache:
    """One Streamlit session and credential scope. Opt-in, references only.

    Student files are never retained between runs. Entries contain no original bytes.
    Striped locks make same-document acquisition single-flight; at most two uploads.
    """
    def __init__(self, scope, ttl=600, capacity=8, clock=time):
        self.scope, self.ttl, self.capacity, self.clock = scope, ttl, capacity, clock
        self.entries = {}
        self.lock = RLock()
        self.cleanup_failures = 0
        self.leases = {}
        self.key_locks = [RLock() for _ in range(32)]
        self.upload_slots = BoundedSemaphore(2)

    def delete(self, client, remote):
        try:
            client.files.delete(name=remote.name)
        except Exception as exc:
            if error_code(exc) != 404:
                self.cleanup_failures += 1

    def evict(self, key):
        item = self.entries.pop(key)
        self.delete(item[2], item[0])

    def prune(self):
        with self.lock:
            for key, item in list(self.entries.items()):
                if not self.leases.get(key) and (self.clock.monotonic() >= item[1] or self.clock.time() >= item[3] - 60):
                    self.evict(key)

    def clear(self):
        with self.lock:
            for key in list(self.entries):
                if self.leases.get(key):
                    item = self.entries[key]
                    self.entries[key] = (item[0], 0, item[2], item[3])
                else:
                    self.evict(key)


class DocumentRun:
    """Owns student/uncacheable uploads until close; reference cache owns retained files."""
    def __init__(self, client, cache, retain=False, progress=None):
        self.client, self.cache, self.retain, self.progress = client, cache, retain, progress
        self.owned, self.acquired = {}, {}
        self.leased = set()
        self.closed = False

    def acquire(self, data, filename, owner, reference=False, progress=None):
        # Role, owner, content, credential/account scope AND session must match.
        key = (self.cache.scope, owner, digest(data))
        callback = progress or self.progress
        cache_allowed = self.retain and reference
        with self.cache.key_locks[hash(key) % len(self.cache.key_locks)]:
            with self.cache.lock:
                if self.closed:
                    raise RuntimeError("Document run is closed")
                if key in self.acquired:
                    return self.acquired[key]
                self.cache.prune()
                item = self.cache.entries.get(key) if self.retain and reference else None
                if item:
                    started = self.cache.clock.monotonic()
                    try:
                        remote = get_status(self.client, item[0].name, started + 30, self.cache.clock)
                    except Exception as exc:
                        if error_code(exc) != 404:
                            raise  # Auth/quota failures must not trigger more uploads.
                        self.cache.entries.pop(key, None)
                    else:
                        expiry = expiration(remote)
                        if (state_of(remote) in {"ACTIVE", "SUCCEEDED"} and expiry is not None
                                and self.cache.clock.time() < expiry - 60
                                and self.cache.clock.monotonic() < item[1]):
                            self.acquired[key] = remote
                            self.cache.leases[key] = self.cache.leases.get(key, 0) + 1
                            self.leased.add(key)
                            if callback:
                                callback({"stage": "Validated cached reference", "elapsed": self.cache.clock.monotonic() - started, "done": True})
                            return remote
                        if self.cache.leases.get(key):
                            cache_allowed = False
                        else:
                            self.cache.evict(key)
            with self.cache.upload_slots:
                remote = transfer(self.client, data, filename, callback, self.cache.clock)
            with self.cache.lock:
                if self.closed:
                    self.cache.delete(self.client, remote)
                    raise RuntimeError("Document run was closed during transfer")
                self.acquired[key] = remote
                expiry = expiration(remote)
                if cache_allowed and expiry is not None and self.cache.clock.time() < expiry - 60:
                    if len(self.cache.entries) >= self.cache.capacity:
                        idle = next((old for old in self.cache.entries if not self.cache.leases.get(old)), None)
                        if idle is None:
                            self.owned[remote.name] = remote
                            return remote
                        self.cache.evict(idle)
                    self.cache.entries[key] = (remote, self.cache.clock.monotonic() + self.cache.ttl, self.client, expiry)
                    self.cache.leases[key] = self.cache.leases.get(key, 0) + 1
                    self.leased.add(key)
                else:
                    self.owned[remote.name] = remote
                return remote

    def close(self):
        with self.cache.lock:
            if self.closed:
                return
            self.closed = True
            for remote in self.owned.values():
                self.cache.delete(self.client, remote)
            self.owned.clear()
            self.acquired.clear()
            for key in self.leased:
                count = self.cache.leases.get(key, 1) - 1
                if count:
                    self.cache.leases[key] = count
                else:
                    self.cache.leases.pop(key, None)
            self.leased.clear()
            self.cache.prune()
