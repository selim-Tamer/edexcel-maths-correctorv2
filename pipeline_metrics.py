"""Content-free stage timings, scoped to one worker with ContextVar isolation."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import uuid

_current = ContextVar('math_marker_pipeline', default=None)
STAGES = {'preparation', 'upload', 'file_processing', 'download', 'generation', 'validation', 'audit'}
MODES = {'manual_student', 'classroom_student', 'manual_preparation', 'document'}


def request_id():
    current = _current.get()
    return current['record']['request_id'] if current else None


def record(stage, seconds):
    current = _current.get()
    if current and stage in STAGES and isinstance(seconds, (int, float)) and math.isfinite(seconds):
        timings = current['record']['stages_seconds']
        timings[stage] = round(timings.get(stage, 0) + max(0, seconds), 6)


def call(stage, function, *args, **kwargs):
    current = _current.get()
    if current is None:
        return function(*args, **kwargs)
    clock = current['clock']; started = clock.monotonic()
    try:
        return function(*args, **kwargs)
    finally:
        record(stage, clock.monotonic()-started)


@contextmanager
def job(mode, clock):
    assert mode in MODES
    parent = request_id()
    data = {'request_id': uuid.uuid4().hex, 'stage': mode, 'stages_seconds': {}, 'outcome': 'running'}
    if parent:
        data['parent_request_id'] = parent
    started = clock.monotonic()
    token = _current.set({'clock': clock, 'record': data})
    try:
        yield data
        if data['outcome'] == 'running':
            data['outcome'] = 'completed'
    except BaseException:
        data['outcome'] = 'failed'
        raise
    finally:
        data['elapsed_seconds'] = round(clock.monotonic()-started, 6)
        _current.reset(token)
        try:
            import json
            import marking_requests
            if marking_requests.diagnostics_enabled():
                marking_requests._console_logger().info('marking_pipeline_metrics %s', json.dumps(data, sort_keys=True))
        except Exception:
            pass  # logging must never affect marks or replace a failure
