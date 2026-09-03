import hashlib
import hmac
import pytest
from app.clients.odoo_client import OdooClient, SEMAFORO_ODOO
from app.core.breaker import CircuitBreaker, CircuitBreakerOpenError


def test_hmac_signature_generation():
    """Verify that OdooClient generates exact HMAC-SHA256 signature accepted by Odoo."""
    client = OdooClient()
    client.secret = "test_shared_secret_123"

    body_bytes = b'{"sku": "MILK001", "qty": 2}'
    headers = client._generate_hmac_headers(body_bytes)

    assert "X-Holi-Timestamp" in headers
    assert "X-Holi-Signature" in headers

    # Verify signature calculation independently
    ts = headers["X-Holi-Timestamp"]
    message = ts.encode('utf-8') + body_bytes
    expected_sig = hmac.new(b"test_shared_secret_123", message, hashlib.sha256).hexdigest()

    assert headers["X-Holi-Signature"] == expected_sig


def test_circuit_breaker_behavior():
    """Verify circuit breaker trips after 5 consecutive failures."""
    breaker = CircuitBreaker("test_breaker", fail_max=5, reset_timeout=2.0)
    assert breaker.state == "CLOSED"

    for _ in range(4):
        breaker.before_call()
        breaker.record_failure()
        assert breaker.state == "CLOSED"

    # 5th failure trips it
    breaker.record_failure()
    assert breaker.state == "OPEN"

    with pytest.raises(CircuitBreakerOpenError):
        breaker.before_call()


@pytest.mark.asyncio
async def test_semaphore_limit():
    """Verify SEMAFORO_ODOO has a hard concurrency limit (Rule 5)."""
    assert SEMAFORO_ODOO._value <= 20
