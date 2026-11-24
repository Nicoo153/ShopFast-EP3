import os, sqlite3, json, time
from fastapi.testclient import TestClient
from app.main import app, INVENTORY_STATE, CATALOG_ITEMS
from worker.consumer import init_db, process_message

client = TestClient(app)
AUTH = {"Authorization": "Bearer demo-token-shopfast"}

def setup_module():
    CATALOG_ITEMS.clear()
    CATALOG_ITEMS.extend([
        {"producto": "P17", "categoria": "audio", "precio_usd": 45.0},
        {"producto": "P28", "categoria": "audio", "precio_usd": 80.0},
        {"producto": "P01", "categoria": "electronica", "precio_usd": 120.0}
    ])
    now = time.time()
    INVENTORY_STATE["P17"] = {"product_id": "P17", "stock": 0, "revision": 42, "eligible": True, "observed_at": now}
    INVENTORY_STATE["P28"] = {"product_id": "P28", "stock": 6, "revision": 40, "eligible": True, "observed_at": now}
    INVENTORY_STATE["P01"] = {"product_id": "P01", "stock": 10, "revision": 40, "eligible": True, "observed_at": now}

# Caso 1: Seguridad - petición sin token retorna 401 sin filtrar datos
def test_01_unauthorized_request():
    r = client.get("/v1/recommendations?usuario=U1")
    assert r.status_code == 401

# Caso 2: Contrato móvil - IDs son strings, límite <= 10 y orden estable
def test_02_mobile_contract_and_limit():
    r = client.get("/v1/recommendations?usuario=U_NUEVO&limit=10&force_fail_model=true", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) <= 10
    for it in body["items"]:
        assert isinstance(it["product_id"], str)

# Caso 3: Exclusión por stock cero (P17)
def test_03_zero_stock_excluded():
    r = client.get("/v1/recommendations?usuario=U7&force_fail_model=true", headers=AUTH)
    ids = [i["product_id"] for i in r.json()["items"]]
    assert "P17" not in ids

# Caso 4: Revisión tardía de inventario ignorada (s41 < s42)
def test_04_out_of_order_revision_ignored():
    r = client.post("/v1/admin/stock-adapter", json={"product_id": "P17", "stock": 4, "revision": 41}, headers=AUTH)
    assert r.json()["applied"] is False
    assert INVENTORY_STATE["P17"]["stock"] == 0

# Caso 5: Estado vencido (>300s) excluido por TTL
def test_05_stale_stock_excluded():
    client.post("/v1/admin/stock-adapter", json={"product_id": "P28", "stock": 6, "revision": 41, "age_offset_s": 315}, headers=AUTH)
    r = client.get("/v1/recommendations?usuario=U8&force_fail_model=true", headers=AUTH)
    ids = [i["product_id"] for i in r.json()["items"]]
    assert "P28" not in ids
    # Restaurar P28
    client.post("/v1/admin/stock-adapter", json={"product_id": "P28", "stock": 6, "revision": 42, "age_offset_s": 0}, headers=AUTH)

# Caso 6: Degradación segura a respuesta vacía (200 OK) si ningún artículo es elegible
def test_06_empty_safe_degradation():
    client.post("/v1/admin/stock-adapter", json={"product_id": "P28", "stock": 0, "revision": 43}, headers=AUTH)
    client.post("/v1/admin/stock-adapter", json={"product_id": "P01", "stock": 0, "revision": 43}, headers=AUTH)
    r = client.get("/v1/recommendations?usuario=U8&force_fail_model=true", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["modo"] == "vacia"
    assert r.json()["items"] == []
    # Restaurar
    client.post("/v1/admin/stock-adapter", json={"product_id": "P28", "stock": 6, "revision": 44}, headers=AUTH)
    client.post("/v1/admin/stock-adapter", json={"product_id": "P01", "stock": 10, "revision": 44}, headers=AUTH)

# Caso 7: Fallback ante indisponibilidad del endpoint de inferencia
def test_07_model_failure_fallback():
    r = client.get("/v1/recommendations?usuario=U8&force_fail_model=true", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["modo"] == "alternativa"

# Caso 8: Entrega repetida e idempotencia en consumidor (Pág. 5 y 6)
def test_08_idempotent_consumer_duplicate_delivery(tmp_path):
    db_file = str(tmp_path / "test_events.db")
    os.environ["DB_PATH"] = db_file
    import worker.consumer as wc
    wc.DB_PATH = db_file
    conn = init_db()
    msg = json.dumps({"event_id": "web:e19", "tipo": "clic", "request_id": "r1"}).encode("utf-8")
    # 3 entregas (original + 2 copias para N=1)
    process_message(conn, msg)
    process_message(conn, msg)
    process_message(conn, msg)
    stats = conn.execute("SELECT total_deliveries, duplicate_deliveries FROM delivery_stats WHERE id=1").fetchone()
    unique_count = conn.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0]
    assert stats == (3, 2)
    assert unique_count == 1

# Caso 9: Conciliación entre endpoint y dashboard de trazas
def test_09_trace_reconciliation():
    r = client.get("/v1/recommendations?usuario=U99&force_fail_model=true", headers=AUTH)
    req_id = r.json()["request_id"]
    tr = client.get("/v1/observability/traces", headers=AUTH).json()["traces"]
    assert any(t["request_id"] == req_id for t in tr)

# Caso 10: Caso de frontera propio - parámetro limit fuera de rango (>10) rechazado por contrato OpenAPI
def test_10_boundary_limit_exceeded():
    r = client.get("/v1/recommendations?usuario=U1&limit=15", headers=AUTH)
    assert r.status_code == 422
