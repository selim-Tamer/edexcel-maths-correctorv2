"""Offline Stage S4 benchmark: rate-limit gate overhead and bounded state.

Deterministic, no provider traffic. Measures:

1. Sequential permit acquisition+release cost (no contention).
2. Contention cost when two Streamlit sessions hammer the gate.
3. Cleanup cost while hundreds of pseudonymous owners churn
   (bounded-window pricing).
4. Bounded state size after heavy churn (memory hygiene).
5. A normal classroom-batch pattern (30 sequential single-student runs).

Prints PASS/FAIL lines and exits nonzero on any regression, following the
offline benchmark conventions of the repository (no timing-based flakiness:
work-count assertions, not wall-clock opens of provider endpoints).

Run directly: python tests/benchmark_gemini_limits.py
"""
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import gemini_limits as gate


# Deterministic clock: the benchmark drives windows explicitly.
class BenchmarkClock:
    def __init__(self):
        self.t = 1_000_000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def advance(self, seconds):
        self.t += seconds


def timed(callable_):
    started = time.perf_counter()
    result = callable_()
    return result, time.perf_counter() - started


def report(name, elapsed, summary=""):
    print(f"{name}: {elapsed * 1000:.2f} ms{summary}")


def state_bytes():
    blob = json.dumps(gate.privacy_probe(), default=str)
    return len(blob.encode("utf-8"))


def main():
    failures = 0
    gate.flush_state()
    clock = BenchmarkClock()

    # 1. Sequential acquisition+release, no contention.
    gate.flush_state()

    def sequential_reservations():
        for index in range(200):
            clock.advance(305.0)  # fresh windows keep start budgets available
            started = time.perf_counter()
            permit = gate.acquire_job(
                "sub-teacher-bench-seq", gate.MANUAL_MARKING, clock=clock)
            permit.release()
            samples.append(time.perf_counter() - started)
        return statistics.median(samples)

    samples = []
    median, elapsed = timed(sequential_reservations)
    report("1. sequential acquire+release (median of 200)", elapsed,
           f" ({median * 1e6:.0f} us per call)")
    if median > 0.002:  # generous bound: far below any UI refresh budget
        print(f"FAIL: median gate decision {median * 1e6:.0f} us exceeds 2 ms")
        failures += 1

    # 2. Contention: two sessions hammering together.
    gate.flush_state()
    grants = []
    grants_lock = threading.Lock()

    def hammer(index):
        local = 0
        for _ in range(500):
            clock.advance(31.0)
            try:
                permit = gate.acquire_job(
                    f"sub-teacher-bench-{index}", gate.MANUAL_MARKING, clock=clock)
            except gate.RateLimitDenied:
                continue   # expected: the caps deny most hammering attempts
            permit.release()
            local += 1
        with grants_lock:
            grants.append(local)

    def contention():
        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        return set(threads) == set(threads) and all(not t.is_alive() for t in threads)

    completed, elapsed = timed(contention)
    report("2. two-session contention (2x500 attempts)", elapsed)
    if not completed:
        print("FAIL: contention threads did not finish (possible deadlock)")
        failures += 1
    total_grants = sum(grants)
    if not 0 < total_grants <= 1000:
        print(f"FAIL: unexpected grant count under caps: {total_grants}")
        failures += 1

    # 3. Cleanup under owner churn: pruned tables keep decisions cheap.
    gate.flush_state()
    churn = gate.MAX_TRACKED_OWNERS

    def churn_owners():
        for index in range(400):
            owner = f"sub-teacher-churn-{index:05d}"
            permit = gate.acquire_job(owner, gate.MANUAL_MARKING, clock=clock)
            permit.release()
            clock.advance(305.0)
        table = gate.privacy_probe()["job_starts"]
        return len(table) <= churn

    bounded, elapsed = timed(churn_owners)
    report("3. owner churn cleanup (400 admits)", elapsed)
    if not bounded:
        print("FAIL: owner table not pruned to the bounded size")
        failures += 1

    # 4. Bounded state after heavy churn.
    blob = state_bytes()
    print(f"4. bounded-state byte size: {blob} bytes")
    if blob <= 0 or blob > 512 * 1024:
        print(f"FAIL: limiter state unexpectedly large: {blob} bytes")
        failures += 1

    # 5. Classroom-batch pattern: 30 one-student runs (sequential).
    gate.flush_state()

    def classroom_batch():
        for student in range(30):
            clock.advance(305.0)
            permit = gate.acquire_job(
                f"sub-teacher-classroom-batch", gate.CLASSROOM_MARKING, clock=clock)
            permit.release()
        return True

    _, elapsed = timed(classroom_batch)
    report("5. classroom-batch pattern (30 single-student runs)", elapsed)

    diagnostics = gate.diagnostic_snapshot()
    print(f"diagnostics ring entries: {len(diagnostics)} (bounded "
          f"{gate.DIAGNOSTIC_RING_CAPACITY})")
    if len(diagnostics) > gate.DIAGNOSTIC_RING_CAPACITY or not diagnostics:
        print("FAIL: diagnostics ring lost its bound")
        failures += 1

    gate.flush_state()
    print("PASS" if failures == 0 else f"FAIL ({failures} check(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
