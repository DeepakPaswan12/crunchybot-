import threading
import time

class EventBus:
    """
    Thread-safe event fan-out.
    Handlers registered via .on(event, fn).
    Events: valid, invalid, retry, error, info, warn, fatal, progress, done
    """
    def __init__(self):
        self._handlers = {}
        self._lock = threading.RLock()

    def on(self, event, handler):
        with self._lock:
            self._handlers.setdefault(event, []).append(handler)

    def emit(self, event, **payload):
        with self._lock:
            handlers = list(self._handlers.get(event, []))
        for h in handlers:
            try:
                h(**payload)
            except Exception:
                pass


class JobUI:
    """
    Per-job UI surface. Wraps EventBus and tracks counters/progress.
    No ANSI, no ticker thread — the bot handles that by polling .snapshot().
    """
    def __init__(self, bus: EventBus, total: int = 0):
        self.bus = bus
        self.lock = threading.RLock()
        self.start_time = time.time()
        self.total = total
        self.counters = {"valid": 0, "invalid": 0, "retry": 0, "error": 0}
        self.current = ""
        self.finished = False
        self.aborted = False

    def _bump(self, key):
        with self.lock:
            self.counters[key] += 1

    def checking(self, email):
        with self.lock:
            self.current = email

    def valid(self, email, tier):
        self._bump("valid")
        self.bus.emit("valid", email=email, tier=tier)

    def invalid(self, email):
        self._bump("invalid")
        self.bus.emit("invalid", email=email)

    def retry(self, email, reason):
        self._bump("retry")
        self.bus.emit("retry", email=email, reason=reason)

    def error(self, email, reason):
        self._bump("error")
        self.bus.emit("error", email=email, reason=reason)

    def info(self, msg):
        self.bus.emit("info", msg=msg)

    def warn(self, msg):
        self.bus.emit("warn", msg=msg)

    def fatal(self, msg):
        self.bus.emit("fatal", msg=msg)

    def done(self):
        self.finished = True
        self.bus.emit("done", snapshot=self.snapshot())

    def snapshot(self):
        with self.lock:
            elapsed = time.time() - self.start_time
            processed = sum(self.counters.values())
            speed = processed / elapsed if elapsed > 0 else 0.0
            remaining = max(self.total - processed, 0)
            eta = remaining / speed if speed > 0 else 0.0
            return {
                "total": self.total,
                "processed": processed,
                "valid": self.counters["valid"],
                "invalid": self.counters["invalid"],
                "retry": self.counters["retry"],
                "error": self.counters["error"],
                "elapsed": elapsed,
                "speed": speed,
                "eta": eta,
                "current": self.current,
                "finished": self.finished,
                "aborted": self.aborted,
            }