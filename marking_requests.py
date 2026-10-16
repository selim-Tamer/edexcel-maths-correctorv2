"""Bounded Gemini marking calls and content-free, server-only diagnostics.

No prompts, file IDs, student identifiers, response text, exception messages or
credentials are recorded. State is owned by the SDK client (one account/batch).
"""
import json
import logging
import math
import os
import random
import re
import sys
import time
import uuid
from threading import Lock, RLock

REQUEST_TIMEOUT = 120
TOTAL_BUDGET = 180
MAX_ATTEMPTS = 2  # non-fast-rejection attempts
MAX_REQUESTS = 4  # absolute cap, including unavailable/quota responses
FAST_REJECTION_SECONDS = 10
SERVICE_RETRY_DELAY = 5
MAX_SERVICE_WAIT = 30
DEADLINE_FALLBACK_TIMEOUT = 45
MIN_DEADLINE_FALLBACK_WINDOW = 15
_creation_lock = Lock()
_diagnostics_lock = RLock()
_diagnostics_enabled = None
_startup_enabled = None
_recovery_attempts = None


def configure_recovery(secrets=None):
    """Server setting only. Hard caps cannot be raised by configuration."""
    global _recovery_attempts
    value = os.getenv('MATH_MARKER_MAX_ATTEMPTS', '3')
    if secrets is not None:
        try:
            value = secrets.get('MATH_MARKER_MAX_ATTEMPTS', value)
        except Exception:
            pass
    _recovery_attempts = parse_attempt_limit(value)
    return _recovery_attempts


def parse_attempt_limit(value):
    return int(str(value)) if str(value) in ('1', '2', '3') else 3


def configured_attempts():
    return (_recovery_attempts if _recovery_attempts is not None else
            parse_attempt_limit(os.getenv('MATH_MARKER_MAX_ATTEMPTS', '3')))


def safe_connection_failure(exc):
    # Connection establishment/pool acquisition precedes sending a generation.
    # Read/write/protocol failures have ambiguous delivery and must not retry.
    if isinstance(exc, ConnectionRefusedError):
        return True
    try:
        import httpx
        return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout))
    except ImportError:
        return False


def provider_deadline(exc):
    """Require a received HTTP response plus Gemini's structured deadline status.

    A bare numeric code, exception text, or local transport timeout is insufficient.
    The failed response contains no usable marks; only one bounded alternative
    may follow. This does not guarantee zero provider computation/quota cost.
    """
    payload = error_payload(exc)
    return (getattr(getattr(exc, 'response', None), 'status_code', None) == 504
            and payload.get('code') == 504
            and payload.get('status') == 'DEADLINE_EXCEEDED')


class MarkingFailure(RuntimeError):
    """Only allowlisted explanations and a random support reference reach UI."""
    def __init__(self, category, request_id):
        explanations = {
            'quota_limited': 'The marking service is rate-limited. Try again later.',
            'timeout': 'The marking service timed out. Review the original PDFs or try again later.',
            'deadline_exceeded': 'The marking time budget was reached. Try again later.',
            'request_rejected': 'The marking service rejected the request. Check API access and uploaded files.',
            'model_unavailable': 'The configured marking services were unavailable. Check model access.',
            'service_unavailable': 'The marking service was temporarily unavailable. Try again later.',
            'invalid_response': 'The marking service returned an unusable result. Review the original PDFs or try again later.',
            'empty_response': 'The marking service returned no result. Review the original PDFs or try again later.',
            'truncated_response': 'The marking response was incomplete. Review the original PDFs.',
            'blocked_response': 'The marking service could not provide a complete response. Review the original PDFs.',
            'cooldown': 'The marking services are temporarily on cooldown. Try again later.',
            'no_models': 'No marking service was configured. Check the app configuration.',
            'transport_or_client_error': 'The marking service could not be reached or the client failed. Try again later.',
            'connection_unavailable': 'The marking service could not be connected to. Try again later.',
        }
        self.category = category if category in explanations else 'transport_or_client_error'
        self.request_id = request_id
        super().__init__('Marking did not complete. No new marks were saved. '
                         + explanations[self.category] + ' Reference: ' + request_id)


def failure(record, category, stop_reason):
    record.update(outcome='failed', failure_category=category, stop_reason=stop_reason)
    return MarkingFailure(category, record['request_id'])


def state_for(client):
    with _creation_lock:
        state = getattr(client, '_math_marking_state', None)
        if state is None:
            state = {'lock': Lock(), 'blocked': {}, 'account_until': 0, 'last': None}
            client._math_marking_state = state
        return state


def diagnostics_enabled():
    # The app resolves Streamlit Secrets on its script thread. Standalone tests
    # and CLI callers can still use the environment without importing Streamlit.
    return (_diagnostics_enabled if _diagnostics_enabled is not None
            else os.getenv('MATH_MARKER_DIAGNOSTICS') == '1')


def _console_logger():
    """Own one INFO stderr handler; do not rely on root/Streamlit configuration."""
    with _diagnostics_lock:
        logger = logging.getLogger('math_marker.marking')
        logger.setLevel(logging.INFO)
        logger.disabled = False
        logger.propagate = False
        owned = [h for h in logger.handlers if getattr(h, '_math_marker_console', False)]
        if not owned or owned[0].stream is not sys.stderr:
            for handler in owned:
                logger.removeHandler(handler)
                handler.close()
            handler = logging.StreamHandler(sys.stderr)
            handler._math_marker_console = True
            handler.setLevel(logging.INFO)
            handler.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(handler)
        return logger


def configure_diagnostics(secrets=None):
    """Resolve only the diagnostic switch, then emit a boolean-only startup check.

    Top-level Streamlit Secrets take precedence. Missing/unavailable secrets
    fall back to the environment. Never echo values, errors or other secrets.
    Called on app startup/rerun, not from marking workers.
    """
    global _diagnostics_enabled, _startup_enabled
    value = os.getenv('MATH_MARKER_DIAGNOSTICS', '')
    if secrets is not None:
        try:
            value = secrets.get('MATH_MARKER_DIAGNOSTICS', value)
        except Exception:
            pass
    enabled = type(value) in (str, int) and str(value).strip() == '1'
    with _diagnostics_lock:
        _diagnostics_enabled = enabled
        if _startup_enabled != enabled:
            try:
                _console_logger().info('marking_diagnostics_startup %s', json.dumps({'enabled': enabled}))
                _startup_enabled = enabled
            except Exception:
                pass  # logging failure must not prevent startup or marking
    return enabled


def emit(record):
    if diagnostics_enabled():
        _console_logger().info('marking_metrics %s', json.dumps(record, sort_keys=True))


def status_code(exc):
    value = getattr(exc, 'code', None) or getattr(exc, 'status_code', None)
    try:
        return int(value)
    except (TypeError, ValueError):
        # Some wrappers omit status attributes. Inspect but never retain their text.
        match = re.search(r'\b(400|401|403|404|408|429|500|502|503|504)\b', str(exc))
        return int(match.group()) if match else None


def error_payload(exc):
    """Read SDK structured errors in memory only; never retain or log them."""
    payload = getattr(exc, 'details', None)
    if not isinstance(payload, dict):
        return {}
    payload = payload.get('error', payload)
    return payload if isinstance(payload, dict) else {}


def model_quota(exc, model):
    """Require every quota violation to identify this model and a per-model limit.

    Missing, mixed, unfamiliar or conflicting scope fails closed. Message text
    alone never authorizes a fallback, even when it mentions a model name.
    """
    details = error_payload(exc).get('details')
    if not isinstance(details, list) or not details:
        return False
    found = False
    for detail in details:
        if not isinstance(detail, dict):
            return False
        kind = detail.get('@type')
        if kind in ('type.googleapis.com/google.rpc.RetryInfo', 'type.googleapis.com/google.rpc.Help'):
            continue
        if kind != 'type.googleapis.com/google.rpc.QuotaFailure':
            return False
        violations = detail.get('violations')
        if not isinstance(violations, list) or not violations:
            return False
        for violation in violations:
            if not isinstance(violation, dict):
                return False
            dimensions = violation.get('quotaDimensions')
            quota_id = violation.get('quotaId')
            metric = violation.get('quotaMetric')
            if (not isinstance(dimensions, dict) or dimensions.get('model') != model
                    or not isinstance(quota_id, str) or 'PerModel' not in quota_id
                    or not isinstance(metric, str)
                    or not metric.startswith('generativelanguage.googleapis.com/generate_content_')):
                return False
            found = True
    return found


def unavailable_model(exc, model):
    # Gemini's standard 404 names the requested models/<id>. A missing file or
    # unidentified 404 must not poison the model cache or earn extra attempts.
    message = error_payload(exc).get('message')
    return (isinstance(message, str)
            and re.search(r'(?<![\w/])models/' + re.escape(model)
                          + r'\s+(?:is\s+)?(?:not found|not supported)\b', message, re.I) is not None)


def response_stats(response, record):
    usage = getattr(response, 'usage_metadata', None)
    version = getattr(response, 'model_version', None)
    if isinstance(version, str) and re.fullmatch(r'gemini-[a-z0-9.-]{1,80}', version):
        record['actual_model_version'] = version
    for key in ('prompt_token_count', 'candidates_token_count', 'thoughts_token_count',
                'cached_content_token_count', 'total_token_count'):
        value = getattr(usage, key, None)
        if type(value) is int and 0 <= value <= 10**9:
            record[key] = value
    modalities = {}
    for detail in getattr(usage, 'prompt_tokens_details', None) or []:
        modality = getattr(detail, 'modality', None)
        modality = getattr(modality, 'value', modality)
        count = getattr(detail, 'token_count', None)
        if modality in ('TEXT', 'IMAGE', 'VIDEO', 'AUDIO', 'DOCUMENT') and type(count) is int and 0 <= count <= 10**9:
            modalities[modality] = count
    if modalities:
        record['prompt_tokens_by_modality'] = modalities
    candidates = getattr(response, 'candidates', None) or []
    finish = getattr(candidates[0], 'finish_reason', None) if candidates else None
    finish = getattr(finish, 'value', finish)
    record['finish_reason'] = finish if finish in ('STOP', 'MAX_TOKENS', 'SAFETY', 'RECITATION', 'OTHER') else 'unknown'
    block = getattr(getattr(response, 'prompt_feedback', None), 'block_reason', None)
    block = getattr(block, 'value', block)
    if block and block != 'BLOCK_REASON_UNSPECIFIED':
        return 'BLOCKED'
    return finish


def service_retry_delay(exc, now, retry_index=0):
    """Honor finite Retry-After seconds/dates; never retain header contents."""
    from document_pipeline import retry_delay
    fallback = min(20, SERVICE_RETRY_DELAY * 2 ** min(retry_index, 2)) + random.uniform(0, 1)
    seconds = retry_delay(exc, fallback, now)
    if not math.isfinite(seconds):
        return fallback
    return max(fallback, seconds)


def generate(client, models, contents, system, schema, config_factory, parse, progress=None, clock=time):
    import pipeline_metrics
    state = state_for(client)
    queued = clock.monotonic()
    # Do not increase marking concurrency even if a caller submits parallel jobs.
    with state['lock']:
        started = clock.monotonic()
        record = {'request_id': pipeline_metrics.request_id() or uuid.uuid4().hex, 'stage': 'marking', 'queue_seconds': round(started-queued, 3),
                  'input_text_characters': len(system) + sum(len(c) for c in contents if isinstance(c, str)),
                  'schema_characters': len(json.dumps(schema, separators=(',', ':'))),
                  'document_count': sum(not isinstance(c, str) for c in contents),
                  'attempts': [], 'cooldown_skips': [], 'sleep_seconds': 0}
        deadline = started + TOTAL_BUDGET
        generation_attempts = 0
        service_wait_until = 0
        recovery_limit = configured_attempts()
        record['configured_attempt_limit'] = recovery_limit
        transient_failures = 0
        deadline_fallback_after = None
        model_list = list(dict.fromkeys(models))

        def attempt_limit():
            # Extra capacity is only for transport/server recovery, not repeated
            # invalid output. Fast model rejections do not consume this budget.
            previous = record['attempts'][-1] if record['attempts'] else {}
            retryable = previous.get('outcome') in ('service_unavailable', 'connection_unavailable', 'model_unavailable', 'quota_limited')
            if deadline_fallback_after is not None:
                return min(recovery_limit, deadline_fallback_after + 1)
            return recovery_limit if retryable or not previous else min(MAX_ATTEMPTS, recovery_limit)

        def wait_until(until):
            delay = max(0, until-clock.monotonic())
            if not delay:
                return
            # All waits combined, including inherited cooldowns, stay bounded.
            if delay > MAX_SERVICE_WAIT-record['sleep_seconds'] or delay >= deadline-clock.monotonic():
                if record['attempts']:
                    record['attempts'][-1]['retry_decision'] = 'deferred_to_manual_retry'
                raise failure(record, 'service_unavailable', 'retry_wait_limit')
            if progress:
                progress('The marking service is busy. Waiting briefly before trying again…')
            tick = clock.monotonic()
            clock.sleep(delay)
            record['sleep_seconds'] += round(clock.monotonic()-tick, 3)
            if clock.monotonic() >= deadline:
                raise failure(record, 'deadline_exceeded', 'deadline_exceeded')
        try:
            if state['account_until'] > started:
                record['cooldown_remaining_seconds'] = round(state['account_until']-started, 3)
                raise failure(record, 'quota_limited', 'account_cooldown')
            if model_list and state.get('service_until', 0) > started:
                record['cooldown_remaining_seconds'] = round(state['service_until']-started, 3)
                wait_until(state['service_until'])
            # A failed student must not make all later students fail immediately.
            # Wait once for the earliest temporary model cooldown, never quota/404.
            if model_list and all(state['blocked'].get(m, 0) > clock.monotonic() for m in model_list):
                temporary = [state['blocked'][m] for m in model_list
                             if state.get('temporary', {}).get(m) == state['blocked'][m]]
                if temporary:
                    wait_until(min(temporary))
            for model in model_list:
                safe_model = model if re.fullmatch(r'gemini-[a-z0-9.-]{1,80}', model) else 'configured-model'
                blocked = state['blocked'].get(model, 0)
                if blocked > clock.monotonic():
                    record['cooldown_skips'].append({'model': safe_model, 'remaining_seconds': round(blocked-clock.monotonic(), 3)})
                    continue
                if (len(record['attempts']) >= MAX_REQUESTS or generation_attempts >= attempt_limit()
                        or clock.monotonic() >= deadline):
                    break
                if deadline_fallback_after is not None and len(record['attempts']) > deadline_fallback_after:
                    break  # even a quick rejection consumes the sole post-504 call
                delay = max(0, service_wait_until - clock.monotonic())
                if delay:
                    previous = record['attempts'][-1]
                    if deadline_fallback_after is None:
                        previous['retry_decision'] = 'delayed_fallback'
                    wait_until(service_wait_until)
                timeout = min(REQUEST_TIMEOUT, deadline-clock.monotonic())
                if deadline_fallback_after is not None:
                    if timeout < MIN_DEADLINE_FALLBACK_WINDOW:
                        raise failure(record, 'timeout', 'insufficient_fallback_budget')
                    timeout = min(timeout, DEADLINE_FALLBACK_TIMEOUT)
                attempt = {'model': safe_model, 'attempt': len(record['attempts'])+1,
                           'timeout_seconds': round(timeout, 3)}
                record['attempts'].append(attempt)
                generation_attempts += 1
                if progress:
                    progress('Marking the original work…' if len(record['attempts']) == 1 else 'Trying an alternative marking service…')
                tick = clock.monotonic()
                api_finished = None
                try:
                    response = client.models.generate_content(model=model, contents=contents,
                        config=config_factory(system_instruction=system, response_mime_type='application/json',
                            response_schema=schema,
                            http_options={'timeout': max(1, int(timeout*1000)), 'retry_options': {'attempts': 1}}))
                    api_finished = clock.monotonic()
                    finish = response_stats(response, attempt)
                    if clock.monotonic() >= deadline:
                        attempt['outcome'] = 'deadline_exceeded'
                        break
                    if finish is not None and finish != 'STOP':
                        attempt['outcome'] = 'truncated_response' if finish == 'MAX_TOKENS' else 'blocked_response'
                        break
                    text = getattr(response, 'text', None)
                    attempt['response_characters'] = len(text) if isinstance(text, str) else 0
                    if not isinstance(text, str) or not text.strip():
                        attempt['outcome'] = 'empty_response'
                        continue
                    try:
                        parse_started = clock.monotonic()
                        result = parse(text)
                        if (not isinstance(result, dict) or not isinstance(result.get('questions'), list)
                                or not result['questions'] or not all(isinstance(q, dict) for q in result['questions'])):
                            raise ValueError('Missing marking results')
                    except (ValueError, TypeError, KeyError):
                        attempt['outcome'] = 'invalid_response'
                        continue
                    finally:
                        attempt['parse_seconds'] = round(clock.monotonic()-parse_started, 3)
                    attempt['outcome'] = 'success'
                    record.update(outcome='success', stop_reason='success')
                    return result, model
                except Exception as exc:
                    code = status_code(exc)
                    if code is not None:
                        attempt['http_status'] = code
                    if code == 404 and unavailable_model(exc, model):
                        state.get('temporary', {}).pop(model, None)
                        attempt['outcome'] = 'model_unavailable'
                        state['blocked'][model] = clock.monotonic() + 86400
                        attempt['cooldown_seconds'] = 86400
                        if clock.monotonic() - tick <= FAST_REJECTION_SECONDS:
                            generation_attempts -= 1
                        continue
                    if code == 429:
                        attempt['outcome'] = 'quota_limited'
                        # Only structured, exclusively model-scoped violations permit
                        # fallback. Shared/unknown scope stops the whole client.
                        from document_pipeline import retry_delay
                        seconds = max(90, retry_delay(exc, 90, clock.time()))
                        if any(x in str(exc).lower() for x in ('perday', 'per day', 'limit: 0')):
                            seconds = max(seconds, 21600)
                        attempt['cooldown_seconds'] = seconds
                        if model_quota(exc, model):
                            state.get('temporary', {}).pop(model, None)
                            attempt['quota_scope'] = 'model'
                            state['blocked'][model] = clock.monotonic() + seconds
                            if clock.monotonic() - tick <= FAST_REJECTION_SECONDS:
                                generation_attempts -= 1
                            continue
                        attempt['quota_scope'] = 'shared_or_unknown'
                        state['account_until'] = clock.monotonic() + seconds
                        break
                    if code in (401, 403, 400, 404):
                        attempt['outcome'] = 'request_rejected'
                        break
                    connection_failure = code is None and safe_connection_failure(exc)
                    if code == 504:
                        confirmed = provider_deadline(exc)
                        attempt['timeout_category'] = 'provider_deadline' if confirmed else 'unconfirmed_504'
                        attempt['outcome'] = 'timeout'
                        if confirmed:
                            # Skip the slow model for later students, without waiting
                            # for this cooldown as though it were a short 503.
                            state['blocked'][model] = clock.monotonic() + 180
                            state.get('temporary', {}).pop(model, None)
                            attempt['cooldown_seconds'] = 180
                            if deadline_fallback_after is None:
                                deadline_fallback_after = len(record['attempts'])
                                service_wait_until = clock.monotonic() + service_retry_delay(exc, clock.time())
                                attempt['retry_decision'] = 'bounded_deadline_fallback'
                                continue
                        attempt['retry_decision'] = 'stop_after_timeout'
                        break
                    if not connection_failure and (isinstance(exc, TimeoutError) or code in (408, 504) or 'timeout' in type(exc).__name__.lower()):
                        attempt['outcome'] = 'timeout'
                        attempt['timeout_category'] = 'ambiguous_transport_timeout'
                        # Ambiguous timeout may already have consumed generation quota.
                        break
                    if code in (500, 502, 503) or connection_failure:
                        if connection_failure:
                            attempt['timeout_category'] = 'connection_establishment'
                        attempt['outcome'] = 'connection_unavailable' if connection_failure else 'service_unavailable'
                        seconds = service_retry_delay(exc, clock.time(), transient_failures)
                        transient_failures += 1
                        state['blocked'][model] = clock.monotonic() + max(30, seconds)
                        state.setdefault('temporary', {})[model] = state['blocked'][model]
                        attempt['cooldown_seconds'] = max(30, seconds)
                        service_wait_until = clock.monotonic() + seconds
                        state['service_until'] = service_wait_until
                        attempt['retry_after_seconds'] = seconds
                        attempt['retry_decision'] = 'no_eligible_fallback'
                        continue
                    attempt['outcome'] = 'transport_or_client_error'
                    break
                finally:
                    attempt['api_response_seconds'] = round((api_finished if api_finished is not None else clock.monotonic())-tick, 3)
                    attempt['response_seconds'] = round(clock.monotonic()-tick, 3)
            last = record['attempts'][-1].get('outcome') if record['attempts'] else None
            terminal = {'timeout', 'request_rejected', 'transport_or_client_error',
                        'blocked_response', 'truncated_response', 'deadline_exceeded'}
            if last in terminal or (last == 'quota_limited' and state['account_until'] > clock.monotonic()):
                stop = last
            elif clock.monotonic() >= deadline:
                stop = 'deadline_exceeded'
            elif len(record['attempts']) >= MAX_REQUESTS:
                stop = 'request_limit'
            elif generation_attempts >= attempt_limit():
                stop = 'attempt_limit'
            else:
                stop = 'models_exhausted'
            if record['attempts'] and record['attempts'][-1].get('http_status') == 503:
                record['attempts'][-1]['retry_decision'] = stop
            category = last or ('cooldown' if record['cooldown_skips'] else 'no_models')
            if stop == 'deadline_exceeded':
                category = stop
            raise failure(record, category, stop)
        finally:
            record['elapsed_seconds'] = round(clock.monotonic()-started, 3)
            record['attempt_count'] = len(record['attempts'])
            record['generation_attempt_count'] = generation_attempts
            record['retry_count'] = max(0, len(record['attempts'])-1)
            state['last'] = record  # bounded to one metadata-only run, never added to reports
            # Diagnostics must never replace either successful marks or the
            # classified failure if a deployment logging handler fails.
            try:
                emit(record)
            except Exception:
                pass
