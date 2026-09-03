import time
import logging

_logger = logging.getLogger(__name__)


class CircuitBreakerOpenError(Exception):
    """Raised when request is blocked because the circuit is OPEN."""
    pass


class CircuitBreaker:
    """
    Prevents repeated calls to an unresponsive external service (Odoo/Redis).
    Threshold: 5 consecutive failures opens circuit for 30 seconds.
    """

    def __init__(self, name: str, fail_max: int = 5, reset_timeout: float = 30.0):
        self.name = name
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self.failure_count = 0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF-OPEN
        self.last_failure_time = 0.0

    def before_call(self):
        now = time.time()
        if self.state == "OPEN":
            if now - self.last_failure_time >= self.reset_timeout:
                self.state = "HALF-OPEN"
                _logger.info("CircuitBreaker '%s' is now HALF-OPEN (testing connection)", self.name)
            else:
                raise CircuitBreakerOpenError(
                    f"Circuit '{self.name}' is OPEN. Service temporarily unavailable."
                )

    def record_success(self):
        if self.state in ("OPEN", "HALF-OPEN"):
            _logger.info("CircuitBreaker '%s' recovered and is now CLOSED", self.name)
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.fail_max:
            if self.state != "OPEN":
                _logger.error("CircuitBreaker '%s' tripped! State is now OPEN for %.1f seconds",
                              self.name, self.reset_timeout)
            self.state = "OPEN"
