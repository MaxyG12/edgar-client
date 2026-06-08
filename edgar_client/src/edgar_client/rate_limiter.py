"""
Token-bucket rate limiter for edgar_client.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Rate Limiting — plain English
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The SEC EDGAR fair-access policy limits each user-agent to roughly
10 requests per second.  Exceed that and EDGAR returns HTTP 429
("Too Many Requests") and may temporarily block your IP address.

RateLimiter enforces this with a "minimum gap" strategy:

  Think of it as a turnstile with a stopwatch attached.
  The stopwatch records when the last person was let through.
  Before you can pass, the turnstile asks: has at least 100 ms
  (1/10th of a second) elapsed since the last person?

      Yes → walk through immediately; stopwatch resets to now.
      No  → wait here until 100 ms have elapsed, then pass.

In code terms:
  min_interval = 1 second ÷ 10 requests = 0.100 seconds

  On each acquire() call:
      now  = time.monotonic()               # wall-clock read
      wait = min_interval - (now - last)    # how much gap is missing
      if wait > 0:
          time.sleep(wait)                  # block until the gap is full
      last = time.monotonic()               # stamp the exit time

Why time.monotonic() instead of time.time()?
  time.time() can jump backwards when the system clock is adjusted
  (NTP, daylight saving, etc.).  A backwards jump would make (now - last)
  negative, so wait > min_interval, and we'd sleep way too long.
  time.monotonic() only ever moves forward, making the math safe.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Thread safety
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Without a lock, two threads sharing one EdgarClient could both
read the same stale "last request time", both compute "enough
time has passed", and both fire requests at the same instant —
effectively doubling the request rate.

The threading.Lock makes the check-and-update atomic:

    Thread A: grabs lock → checks stopwatch → sleeps if needed
              → resets stopwatch → releases lock
    Thread B: waits at lock → grabs it after A releases
              → sees A's updated timestamp → computes correct gap

This serialises concurrent calls so they always queue up at the
correct 100 ms spacing, even when multiple threads share one client.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Why "minimum gap" rather than a token bucket?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A token bucket allows short bursts (e.g. 10 tokens → fire 10 requests
instantly if the bucket is full).  A minimum-gap limiter enforces
strict spacing with no bursting.

For EDGAR the distinction rarely matters — we make one request
and wait for the JSON before the next call, so there is no real
bursting opportunity.  The simpler approach is clearer and correct.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Enforces a minimum inter-request interval.

    One instance lives on every EdgarSession and is shared across all calls
    made by that session.  Placing it at the session level (rather than
    per-call) is critical: if each request created its own RateLimiter
    it would always see "last_request_time = 0" and never sleep — defeating
    the whole point.

    Parameters
    ----------
    max_per_second:
        Maximum request rate.  Defaults to 10, matching EDGAR's stated limit.
        Reduce this value for extra-polite crawling.
    """

    def __init__(self, max_per_second: float = 10.0) -> None:
        self.max_per_second: float = max_per_second
        # min_interval is stored as an attribute (not derived on each call)
        # so tests can set it to 0.0 to disable throttling without subclassing.
        self.min_interval: float = 1.0 / max_per_second
        self._last_request_time: float = 0.0
        self._lock: threading.Lock = threading.Lock()

    def acquire(self) -> None:
        """Block until the minimum inter-request gap has elapsed.

        Callers should invoke this once before every outbound HTTP request.
        The method returns as soon as it is safe to send without violating
        the rate limit.

        Under the lock we:
          1. Read the current monotonic clock.
          2. Compute how long we still need to wait.
          3. Sleep if that duration is positive.
          4. Record the exit time as the new "last request" timestamp.

        Step 4 stamps the *exit* time (after the sleep), not the entry time.
        This means consecutive calls always wait at least min_interval between
        their exits — i.e. between the moments each request is actually sent.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            wait = self.min_interval - elapsed
            if wait > 0:
                time.sleep(wait)
            # Re-read after the sleep so we stamp the true send time.
            self._last_request_time = time.monotonic()

    def __repr__(self) -> str:
        return (
            f"<RateLimiter {self.max_per_second:.0f} req/s "
            f"interval={self.min_interval * 1000:.0f}ms>"
        )
