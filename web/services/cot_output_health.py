"""When a CoT push output should back off, pause, be rechecked, and email its creator.

Pure decision logic with an injectable clock; cot_pipeline does the I/O and reports each
connect attempt's outcome here. Live failure state is in memory (a dashboard restart starts
a fresh window); the paused flag and email bookkeeping persist in the output's config.

- A transient failure (refused, timeout, reset or EOF mid-handshake) only pauses once the
  output has failed continuously for PAUSE_AFTER_SECONDS; until then retries back off.
- A definite TLS rejection (an SSLError such as an unknown/expired certificate alert) pauses
  after REJECTIONS_TO_PAUSE in a row.
- Paused outputs are rechecked often at first, then less often.
- The pause email goes out at most once per output per EMAIL_MIN_INTERVAL_SECONDS.
"""

import os
import ssl
import time
from dataclasses import dataclass
from enum import Enum


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


PAUSE_AFTER_SECONDS = _env_int("COT_PAUSE_AFTER_SECONDS", 300)
REJECTIONS_TO_PAUSE = 3
BACKOFF_MIN_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 30.0
RECHECK_FAST_SECONDS = 120
RECHECK_FAST_WINDOW_SECONDS = 1800
RECHECK_SLOW_SECONDS = 600
EMAIL_MIN_INTERVAL_SECONDS = _env_int("COT_PAUSE_EMAIL_MIN_INTERVAL_SECONDS", 6 * 3600)


class ConnectOutcome(Enum):
    OK = "ok"
    TRANSIENT = "transient"
    REJECTED = "rejected"


# SSLError subclasses raised when the connection drops mid-handshake, not by a TLS verdict.
_SSL_INTERRUPTED = (ssl.SSLEOFError, ssl.SSLSyscallError, ssl.SSLZeroReturnError)


def classify_handshake_error(exc):
    """REJECTED for a TLS verdict (alert, bad cert/key, wrong protocol); else TRANSIENT."""
    if isinstance(exc, ssl.SSLError) and not isinstance(exc, _SSL_INTERRUPTED):
        return ConnectOutcome.REJECTED
    return ConnectOutcome.TRANSIENT


@dataclass
class _Failing:
    since: float
    rejections: int = 0
    backoff: float = BACKOFF_MIN_SECONDS
    next_attempt_at: float = 0.0


class CotOutputHealth:
    """Per-output failure window and retry backoff for outputs that are not paused."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._failing = {}

    def should_attempt(self, output_id):
        state = self._failing.get(output_id)
        return state is None or self._clock() >= state.next_attempt_at

    def record(self, output_id, outcome):
        """Record a connect attempt. Returns a pause reason, or None to keep trying."""
        if outcome is ConnectOutcome.OK:
            self._failing.pop(output_id, None)
            return None
        now = self._clock()
        state = self._failing.get(output_id)
        if state is None:
            state = self._failing[output_id] = _Failing(since=now)
        else:
            state.backoff = min(state.backoff * 2, BACKOFF_MAX_SECONDS)
        state.next_attempt_at = now + state.backoff
        state.rejections = state.rejections + 1 if outcome is ConnectOutcome.REJECTED else 0
        if state.rejections >= REJECTIONS_TO_PAUSE:
            return f"the TAK server rejected the TLS handshake {state.rejections} times in a row"
        if now - state.since >= PAUSE_AFTER_SECONDS:
            return f"the TAK server was unreachable for {int((now - state.since) // 60)} minutes"
        return None

    def reset(self, output_id):
        self._failing.pop(output_id, None)


def recheck_due(config, now=None):
    """True when a paused output should be retried (wall-clock times from its config)."""
    now = time.time() if now is None else now
    paused_at = config.get("cot_tls_paused_at") or 0.0  # absent on pauses from older versions
    interval = RECHECK_FAST_SECONDS if now - paused_at < RECHECK_FAST_WINDOW_SECONDS else RECHECK_SLOW_SECONDS
    return now - (config.get("cot_tls_last_check") or 0.0) >= interval


def pause_email_due(config, now=None):
    last = config.get("cot_pause_last_email_at")
    if not last:
        return True
    now = time.time() if now is None else now
    return now - last >= EMAIL_MIN_INTERVAL_SECONDS


def paused_config(now=None):
    """Config patch that pauses an output (shared by the pipeline and Test TLS)."""
    now = time.time() if now is None else now
    return {"cot_tls_paused": True, "cot_tls_paused_at": now, "cot_tls_last_check": now, "cot_pause_emailed": False}
