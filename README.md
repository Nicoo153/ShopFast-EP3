# ShopFast — Sistema de Recomendación con IA (EP3 · ITY1102)
**Equipo:** Maykol Aguirre, Gabriel González, Lukas Gómez y Nicolás Rain | **Fecha:** 26 de noviembre de 2026

## Arquitectura Operativa y Fronteras
- **Inferencia Cloud Compatible (`sagemaker_runtime/`):** Implementa el contrato oficial de AWS SageMaker Runtime (`GET /ping` y `POST /invocations`) con verificación de `schema_version` y desempate determinista.
- **API REST Segura (`app/main.py`):** FastAPI con autenticación JWT (`Bearer demo-token-shopfast`), limitación de tasa (HTTP 429), caché de candidatos con revalidación obligatoria de inventario (`TTL <= 300s`, orden monótono de revisiones) y degradación controlada (`personalizada` -> `alternativa` -> `vacia`).
- **Broker y Consumidor Idempotente (`worker/consumer.py`):** RabbitMQ durable con `basic_ack` posterior a persistencia y deduplicación por `event_id` (`INSERT OR IGNORE`).
- **Dashboard (`dashboard/app.py`):** Trazabilidad extremo a extremo por `request_id` y conciliación entre entregas del broker y efectos únicos.


