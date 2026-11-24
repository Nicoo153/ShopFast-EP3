import os, hashlib, joblib
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any

app = FastAPI(title="ShopFast AWS SageMaker Runtime Compatible Endpoint", version="1.0.0")
MODEL_PATH = os.getenv("MODEL_PATH", "/app/artifacts/model_r2.joblib")
EXPECTED_SCHEMA = os.getenv("EXPECTED_SCHEMA", "1.0")
ARTIFACT = None

class InferencePayload(BaseModel):
    schema_version: str = "1.0"
    usuario: str
    contexto: str
    hora: int
    candidates: List[Dict[str, Any]]

@app.on_event("startup")
def load_model():
    global ARTIFACT
    if os.path.exists(MODEL_PATH):
        ARTIFACT = joblib.load(MODEL_PATH)
        if ARTIFACT.get("schema_version") != EXPECTED_SCHEMA:
            raise RuntimeError(f"Incompatible model schema: {ARTIFACT.get('schema_version')} != {EXPECTED_SCHEMA}")

@app.get("/ping")
def ping():
    if ARTIFACT is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {"status": "Healthy", "model_version": ARTIFACT["model_version"]}

@app.post("/invocations")
def invocations(payload: InferencePayload):
    if ARTIFACT is None:
        raise HTTPException(status_code=503, detail="Model artifact unavailable")
    if payload.schema_version != ARTIFACT.get("schema_version"):
        raise HTTPException(status_code=422, detail="Incompatible model schema_version")
    if not payload.candidates:
        return {"model_version": ARTIFACT["model_version"], "predictions": []}

    stats = ARTIFACT["stats"]
    g_ctr = stats["global_ctr"]
    cat_col = stats["cat_col"]
    rows = []
    for c in payload.candidates:
        prod = c["producto"]
        cat = c["categoria"]
        rows.append({
            "contexto": payload.contexto,
            cat_col: cat,
            "precio_usd": float(c["precio_usd"]),
            "hora": int(payload.hora),
            "misma_cat": int(payload.contexto == cat),
            "u_ctr": stats["u_ctr"].get(payload.usuario, g_ctr),
            "p_ctr": stats["p_ctr"].get(prod, g_ctr)
        })
    df_in = pd.DataFrame(rows)
    probs = ARTIFACT["pipeline"].predict_proba(df_in[ARTIFACT["features"]])[:, 1]
    scored = []
    for c, p in zip(payload.candidates, probs):
        scored.append({"product_id": str(c["producto"]), "score": round(float(p), 6)})
    # Desempate determinista por score descendente y product_id ascendente
    scored.sort(key=lambda x: (-x["score"], x["product_id"]))
    return {"model_version": ARTIFACT["model_version"], "predictions": scored}
