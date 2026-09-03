# HOLI Edge Gateway

API Gateway de alto rendimiento para la aplicación móvil y web de **HOLI Supermercado**, desarrollado con **FastAPI** y respaldado por **Redis Cloud** y **Odoo 18**.

## Arquitectura

- **Edge / Cache First**: Consultas de catálogo, precios y carritos resueltos en sub-milisegundos desde Redis.
- **Circuit Breaker & Fallback**: Degradación elegante ante indisponibilidad de Odoo o servicios externos.
- **Seguridad HMAC**: Comunicación autenticada con endpoints internos de Odoo 18 mediante firmas HMAC-SHA256.
- **Autenticación JWT**: Tokens de acceso locales de 15 minutos y refresh tokens rotativos en Redis.
- **Idempotencia**: Deduplicación de transacciones y pedidos críticos mediante `Idempotency-Key` en Redis.

## Requisitos

- Python >= 3.12
- Redis >= 5.0.0
- Odoo 18 (con módulo `holi_ecommerce_sync`)

## Instalación y Ejecución Local

1. Copiar las variables de entorno:
   ```bash
   cp .env.example .env
   ```
2. Instalar dependencias:
   ```bash
   pip install -e .
   ```
3. Iniciar el servidor de desarrollo:
   ```bash
   uvicorn app.main:app --reload --port 8080
   ```

## Ejecución con Docker

```bash
docker build -t holi-edge-gateway .
docker run -p 8080:8080 --env-file .env holi-edge-gateway
```
