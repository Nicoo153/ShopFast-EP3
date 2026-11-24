import os, time, uuid, json, logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import jwt, requests, pika
from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shopfast-api")

JWT_SECRET = os.getenv("JWT_SECRET", "shopfast-ep3-demo-secret-key")
SAGEMAKER_URL = os.getenv("SAGEMAKER_URL", "http://sagemaker:8080/invocations")
RABBIT_HOST = os.getenv("RABBIT_HOST", "rabbitmq")
STOCK_TTL_SECONDS = 300

app = FastAPI(
    title="ShopFast Recommendation API v1",
    version="1.0.0",
    description="API distribuida de recomendaciones con filtro estricto de inventario autorizado (TTL <= 300s), degradación segura y telemetría asíncrona."
)

# Estado en memoria para inventario autorizado (simula adaptador de servicio comercial REST solo lectura)
# Regla de contrato: revisión monótona por producto, TTL <= 300s, falla cerrada ante omisión o vencimiento.
INVENTORY_STATE: Dict[str, Dict[str, Any]] = {}
CATALOG_ITEMS: List[Dict[str, Any]] = []
RECOMMENDATION_CACHE: Dict[str, Dict[str, Any]] = {}
AUDIT_TRACES: List[Dict[str, Any]] = []
RATE_LIMIT_BUCKET: Dict[str, List[float]] = {}

@app.on_event("startup")
def init_catalog_and_stock():
    global CATALOG_ITEMS, INVENTORY_STATE
    cat_path = os.getenv("CATALOG_PATH", "/app/data/catalogo.csv")
    now = time.time()
    if os.path.exists(cat_path):
        import pandas as pd
        df = pd.read_csv(cat_path)
        CATALOG_ITEMS = df.to_dict(orient="records")
        for idx, row in enumerate(CATALOG_ITEMS):
            pid = str(row["producto"])
            # P17 agotado para demostrar exclusión determinista; resto activo con stock > 0
            stock_val = 0 if pid == "P17" else 15
            INVENTORY_STATE[pid] = {
                "product_id": pid,
                "stock": stock_val,
                "revision": 40,
                "eligible": True,
                "observed_at": now
            }

def verify_jwt(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")
    token = authorization.split(" ", 1)[1]
    if token == "demo-token-shopfast":
        return "bff-client-authorized"
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("sub", "authorized-client")
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid or expired token")

def check_rate_limit(client_id: str, max_per_sec: int = 100):
    now = time.time()
    window = [t for t in RATE_LIMIT_BUCKET.get(client_id, []) if now - t < 1.0]
    if len(window) >= max_per_sec:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    window.append(now)
    RATE_LIMIT_BUCKET[client_id] = window

def publish_event_rabbitmq(event_payload: dict) -> bool:
    try:
        params = pika.ConnectionParameters(host=RABBIT_HOST, connection_attempts=1, socket_timeout=0.5)
        conn = pika.BlockingConnection(params)
        ch = conn.channel()
        ch.queue_declare(queue="shopfast.events", durable=True)
        ch.basic_publish(
            exchange="",
            routing_key="shopfast.events",
            body=json.dumps(event_payload),
            properties=pika.BasicProperties(delivery_mode=2, message_id=event_payload["event_id"])
        )
        conn.close()
        return True
    except Exception as exc:
        logger.warning(f"Broker unavailable (non-blocking): {exc}")
        return False

def filter_eligible_inventory(scored_items: List[Dict[str, Any]], now_ts: float):
    valid, excluded = [], []
    for item in scored_items:
        pid = str(item["product_id"])
        st = INVENTORY_STATE.get(pid)
        if st is None:
            excluded.append({"product_id": pid, "reason": "MISSING_IN_INVENTORY"})
            continue
        age = now_ts - st["observed_at"]
        if age > STOCK_TTL_SECONDS:
            excluded.append({"product_id": pid, "reason": f"STALE_STOCK_AGE_{int(age)}s"})
            continue
        if not st.get("eligible", False) or st.get("stock", 0) <= 0:
            excluded.append({"product_id": pid, "reason": "OUT_OF_STOCK_OR_INELIGIBLE"})
            continue
        valid.append({
            "product_id": pid,
            "score": item["score"],
            "stock_age_s": round(age, 1),
            "revision": st["revision"]
        })
    return valid, excluded

@app.get("/live")
def live():
    return {"status": "alive"}

@app.get("/ready")
def ready():
    try:
        r = requests.get(SAGEMAKER_URL.replace("/invocations", "/ping"), timeout=0.5)
        if r.status_code == 200:
            return {"status": "ready", "sagemaker": r.json()}
    except Exception:
        pass
    raise HTTPException(status_code=503, detail="SageMaker endpoint not ready")

class StockUpdate(BaseModel):
    product_id: str
    stock: int
    revision: int
    eligible: bool = True
    age_offset_s: int = 0

@app.post("/v1/admin/stock-adapter")
def update_stock_adapter(upd: StockUpdate, authorization: Optional[str] = Header(None)):
    verify_jwt(authorization)
    pid = str(upd.product_id)
    current = INVENTORY_STATE.get(pid)
    # Regla de contrato: revisión solo comparable dentro de un mismo producto; ignorar revisiones atrasadas
    if current and upd.revision < current["revision"]:
        return {"applied": False, "reason": "OUT_OF_ORDER_REVISION", "current_revision": current["revision"]}
    INVENTORY_STATE[pid] = {
        "product_id": pid,
        "stock": upd.stock,
        "revision": upd.revision,
        "eligible": upd.eligible,
        "observed_at": time.time() - upd.age_offset_s
    }
    return {"applied": True, "state": INVENTORY_STATE[pid]}

@app.get("/v1/recommendations")
def get_recommendations(
    usuario: str = Query(..., description="ID seudonimizado del usuario"),
    contexto: str = Query("electronica", description="Categoría o superficie de navegación"),
    limit: int = Query(10, ge=1, le=10),
    use_cache: bool = Query(True),
    force_fail_model: bool = Query(False),
    authorization: Optional[str] = Header(None)
):
    t0 = time.perf_counter()
    client_id = verify_jwt(authorization)
    check_rate_limit(client_id)
    request_id = str(uuid.uuid4())
    now_ts = time.time()
    hora = datetime.now(timezone.utc).hour
    cache_key = f"{usuario}:{contexto}:{hora}"

    mode = "personalizada"
    model_version = "r2-logreg-v1.0"
    cache_hit = False
    raw_scored = []

    if use_cache and cache_key in RECOMMENDATION_CACHE and not force_fail_model:
        cache_hit = True
        raw_scored = RECOMMENDATION_CACHE[cache_key]["predictions"]
        model_version = RECOMMENDATION_CACHE[cache_key]["model_version"]
    else:
        try:
            if force_fail_model:
                raise RuntimeError("Simulated SageMaker inference failure")
            resp = requests.post(
                SAGEMAKER_URL,
                json={"schema_version": "1.0", "usuario": usuario, "contexto": contexto, "hora": hora, "candidates": CATALOG_ITEMS},
                timeout=0.35
            )
            resp.raise_for_status()
            data = resp.json()
            raw_scored = data["predictions"]
            model_version = data["model_version"]
            if use_cache:
                RECOMMENDATION_CACHE[cache_key] = {"predictions": raw_scored, "model_version": model_version}
        except Exception as exc:
            logger.error(f"[{request_id}] Inference fallback triggered: {exc}")
            # Fallback seguro: popularidad determinista del catálogo sin inferencia ML
            mode = "alternativa"
            model_version = "fallback-popularity-v1"
            raw_scored = [{"product_id": str(c["producto"]), "score": 0.05} for c in sorted(CATALOG_ITEMS, key=lambda x: str(x["producto"]))]

    # Filtro obligatorio de inventario vigente en ambas rutas (directa y caché)
    valid_items, excluded_items = filter_eligible_inventory(raw_scored, now_ts)
    final_items = valid_items[:limit]
    if not final_items:
        mode = "vacia"

    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    trace_record = {
        "request_id": request_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "usuario_hash": str(abs(hash(usuario)) % 10**8),
        "contexto": contexto,
        "modo": mode,
        "model_version": model_version,
        "cache_hit": cache_hit,
        "latency_ms": latency_ms,
        "returned_count": len(final_items),
        "excluded_count": len(excluded_items),
        "excluded_sample": excluded_items[:5]
    }
    AUDIT_TRACES.append(trace_record)
    if len(AUDIT_TRACES) > 500:
        AUDIT_TRACES.pop(0)

    # Publicación asíncrona de evento de exposición al broker
    event_id = f"imp-{request_id}"
    broker_ok = publish_event_rabbitmq({
        "event_id": event_id,
        "schema_version": "1.0",
        "occurred_at": trace_record["timestamp_utc"],
        "tipo": "impresion",
        "request_id": request_id,
        "model_version": model_version,
        "modo": mode,
        "items": [x["product_id"] for x in final_items]
    })

    return {
        "request_id": request_id,
        "event_id": event_id,
        "broker_published": broker_ok,
        "modo": mode,
        "model_version": model_version,
        "cache_hit": cache_hit,
        "latency_ms": latency_ms,
        "items": final_items,
        "excluded_diagnostics": excluded_items[:5]
    }

@app.get("/v1/observability/traces")
def get_traces(authorization: Optional[str] = Header(None)):
    verify_jwt(authorization)
    return {"total": len(AUDIT_TRACES), "traces": list(reversed(AUDIT_TRACES[-50:]))}
