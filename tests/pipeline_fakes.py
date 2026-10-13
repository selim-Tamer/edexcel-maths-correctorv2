"""Virtual time and a synthetic provider; no network access or model usage."""
from datetime import datetime, timezone
from threading import Lock
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.sleeps = []
    def monotonic(self):
        return self.seconds
    def time(self):
        return 1800000000 + self.seconds
    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.seconds += seconds

class ApiError(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__("Synthetic service failure")
        self.code = code
        self.response = SimpleNamespace(headers={} if retry_after is None else {"Retry-After": retry_after})

class Provider:
    def __init__(self, clock, transfer=2, processing=10, status_time=0):
        self.clock, self.transfer_time, self.processing, self.status_time = clock, transfer, processing, status_time
        self.ready, self.data, self.paths = {}, {}, []
        self.lock=Lock()
        self.files = SimpleNamespace(upload=Mock(side_effect=self.upload), get=Mock(side_effect=self.get), delete=Mock())
    def remote(self, name, state=None, expiry=3600):
        return SimpleNamespace(name=name,
            state=SimpleNamespace(name=state or ("ACTIVE" if self.clock.monotonic() >= self.ready[name] else "PROCESSING")),
            expiration_time=datetime.fromtimestamp(self.clock.time()+expiry, timezone.utc))
    def upload(self, *, file, config=None):
        self.paths.append(file)
        with self.lock:
            name = "files/synthetic-" + str(len(self.data))
            self.data[name] = Path(file).read_bytes()
        self.clock.sleep(self.transfer_time)
        self.ready[name] = self.clock.monotonic() + self.processing
        return self.remote(name)
    def get(self, *, name, config=None):
        self.clock.sleep(self.status_time)
        return self.remote(name)

def uploaded(data=b"%PDF-synthetic", name="synthetic.pdf"):
    return SimpleNamespace(name=name, size=len(data), getvalue=lambda: data)


def model_unavailable(model):
    error = ApiError(404)
    error.details = {'error': {'message': f'models/{model} is not found for API version v1beta, or is not supported for generateContent.'}}
    return error


def quota_failure(model, scope='model', retry_after=None):
    error = ApiError(429, retry_after)
    violation = {'quotaMetric': 'generativelanguage.googleapis.com/generate_content_free_tier_requests',
                 'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier',
                 'quotaDimensions': {'model': model, 'location': 'global'}}
    if scope == 'shared':
        violation['quotaId'] = 'GenerateRequestsPerMinutePerProject-FreeTier'
        violation['quotaDimensions'].pop('model')
    details = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [violation]}]
    error.details = {'error': {'message': 'synthetic private quota response', 'details': details}}
    if scope == 'unknown':
        error.details['error']['details'] = []
    return error
