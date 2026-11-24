import os, sqlite3, json
import requests, pandas as pd
import streamlit as st

API_URL = os.getenv("API_URL", "http://api:8000")
DB_PATH = os.getenv("DB_PATH", "/data/analytics.db")
HEADERS = {"Authorization": "Bearer demo-token-shopfast"}

st.set_page_config(page_title="ShopFast IA — Panel de Gobierno y Operación", layout="wide")
st.title("ShopFast — Control de Lanzamiento, Trazabilidad y Observabilidad IA")

col1, col2 = st.columns([1, 2])
with col1:
    st.subheader("1. Prueba en Vivo (Invocación REST)")
    usuario = st.text_input("Usuario seudonimizado", value="U8")
    contexto = st.selectbox("Contexto / Categoría", ["electronica", "audio", "computacion", "hogar", "accesorios"])
    use_cache = st.checkbox("Usar caché de candidatos", value=True)
    force_fail = st.checkbox("Simular caída de SageMaker (Fallback)", value=False)
    if st.button("Solicitar Recomendaciones"):
        r = requests.get(
            f"{API_URL}/v1/recommendations",
            params={"usuario": usuario, "contexto": contexto, "use_cache": use_cache, "force_fail_model": force_fail},
            headers=HEADERS,
            timeout=3
        )
        st.json(r.json())

with col2:
    st.subheader("2. Trazas de Inferencia (API + SageMaker + Filtro Stock)")
    try:
        tr = requests.get(f"{API_URL}/v1/observability/traces", headers=HEADERS, timeout=2).json()
        df_tr = pd.DataFrame(tr.get("traces", []))
        if not df_tr.empty:
            st.dataframe(df_tr[["timestamp_utc", "request_id", "modo", "model_version", "cache_hit", "latency_ms", "returned_count", "excluded_count"]], use_container_width=True)
        else:
            st.info("Aún no hay peticiones registradas en esta sesión.")
    except Exception as e:
        st.error(f"No se pudo conectar con API: {e}")

st.divider()
st.subheader("3. Efectos Persistidos en Almacén Analítico (Consumidor Idempotente RabbitMQ)")
if os.path.exists(DB_PATH):
    conn = sqlite3.connect(DB_PATH)
    stats = pd.read_sql_query("SELECT * FROM delivery_stats", conn)
    events = pd.read_sql_query("SELECT event_id, occurred_at, processed_at, tipo, request_id, model_version, modo FROM processed_events ORDER BY processed_at DESC LIMIT 20", conn)
    c1, c2, c3 = st.columns(3)
    tot_del = int(stats["total_deliveries"].iloc[0]) if not stats.empty else 0
    dup_del = int(stats["duplicate_deliveries"].iloc[0]) if not stats.empty else 0
    c1.metric("Entregas recibidas desde Broker", tot_del)
    c2.metric("Efectos únicos persistidos", tot_del - dup_del)
    c3.metric("Duplicados absorbidos (Idempotencia)", dup_del)
    st.dataframe(events, use_container_width=True)
else:
    st.info("Base analítica esperando primeros eventos del worker.")
