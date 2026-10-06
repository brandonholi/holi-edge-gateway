# HOLI Edge Gateway

Public API `/v1` consumed by the HOLI mobile app. The contract is [docs/contrato-api-v1.md](../docs/contrato-api-v1.md); the API conventions (naming, lists, pagination, errors, status codes) are its **section 0-ter**, the single source of truth. Read it before adding or changing an endpoint.

## Adding or changing an endpoint

1. Declare the response as a Pydantic model with `response_model=`, built on `app/schemas/common.py`: `Lista[T]` for a list, `ListaPaginada[T]` for one that grows without bound, `MensajeOut` for an action with nothing to return. Public field and query names are Spanish `snake_case`; the router translates from the English names Redis and Odoo use (ADR-012).
2. Take common headers from `app/core/deps.py` by type: `SedeRequerida`, `SedeOpcional`, `IdempotencyKey`, `RequestId`.
3. Answer every error with `raise ApiError("<codigo>", detalle?, **extra)` from `app/core/errors.py`. Let `OdooClientError`, `OtpProviderError` and `CircuitBreakerOpenError` propagate: the handlers there translate them. A new error code goes into `CATALOGO` and into section 4 of the contract in the same change.
4. Add the route to the tables in section 2 of the contract, and log any incompatible change in its **Historial**.

Done when `pytest` is green, including `tests/test_convenciones_api.py`, which walks the generated OpenAPI and rejects any route outside the conventions. When it rejects a new array field that is part of a resource rather than the list itself (like a cart's `lineas`), add it to `COLECCIONES_DE_RECURSO` there.

## Environment

Run tests with a venv of this project (`pip install -e ".[dev]"`). The shared `d:\Instancias\holi\.venv` of Odoo ships a different `jwt` package and 12 auth tests fail there.
