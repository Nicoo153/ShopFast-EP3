import os, json, hashlib, joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

DATA_DIR = os.getenv("DATA_DIR", "data")
MODEL_DIR = os.getenv("MODEL_DIR", "artifacts")
os.makedirs(MODEL_DIR, exist_ok=True)

def load_data():
    imp = pd.read_csv(os.path.join(DATA_DIR, "impresiones.csv"))
    cat = pd.read_csv(os.path.join(DATA_DIR, "catalogo.csv"))
    df = imp.merge(cat, on="producto", how="left", suffixes=("", "_cat"))
    df["ts_imp_dt"] = pd.to_datetime(df["ts_impresion"], utc=True)
    df["dia"] = df["ts_imp_dt"].dt.day
    df["hora"] = df["ts_imp_dt"].dt.hour
    df["clic"] = df["ts_clic"].notna().astype(int)
    return df, cat

def build_features(df, train_stats=None):
    cat_col = "categoria_cat" if "categoria_cat" in df.columns else "categoria"
    df = df.copy()
    df["misma_cat"] = (df["contexto"] == df[cat_col]).astype(int)
    if train_stats is None:
        global_ctr = float(df["clic"].mean())
        pop = df.groupby("producto")["clic"].agg(["sum", "count"])
        pop["p_ctr"] = (pop["sum"] + 5 * global_ctr) / (pop["count"] + 5)
        usr = df.groupby("usuario")["clic"].agg(["sum", "count"])
        usr["u_ctr"] = (usr["sum"] + 3 * global_ctr) / (usr["count"] + 3)
        train_stats = {
            "global_ctr": global_ctr,
            "p_ctr": pop["p_ctr"].to_dict(),
            "u_ctr": usr["u_ctr"].to_dict(),
            "cat_col": cat_col
        }
    g_ctr = train_stats["global_ctr"]
    df["p_ctr"] = df["producto"].map(train_stats["p_ctr"]).fillna(g_ctr)
    df["u_ctr"] = df["usuario"].map(train_stats["u_ctr"]).fillna(g_ctr)
    return df, train_stats

def eval_ranking(df_eval, score_col, k_top=3, ndcg_k=10):
    df_e = df_eval.copy()
    df_e["rank_pred"] = df_e.groupby("sesion")[score_col].rank(ascending=False, method="first")
    df_e["pred_bin"] = (df_e["rank_pred"] <= k_top).astype(int)
    acc = accuracy_score(df_e["clic"], df_e["pred_bin"])
    prec = precision_score(df_e["clic"], df_e["pred_bin"], zero_division=0)
    rec = recall_score(df_e["clic"], df_e["pred_bin"], zero_division=0)
    f1 = f1_score(df_e["clic"], df_e["pred_bin"], zero_division=0)
    ndcgs, no_pos = [], 0
    for _, g in df_e.groupby("sesion"):
        if g["clic"].sum() == 0:
            no_pos += 1
            continue
        g_top = g.sort_values("rank_pred").head(ndcg_k)
        rels = g_top["clic"].values
        dcg = np.sum(rels / np.log2(np.arange(2, len(rels) + 2)))
        ideal = np.sort(g["clic"].values)[::-1][:ndcg_k]
        idcg = np.sum(ideal / np.log2(np.arange(2, len(ideal) + 2)))
        ndcgs.append(float(dcg / idcg) if idcg > 0 else 0.0)
    return {
        "accuracy": round(float(acc), 4),
        "precision_at_3": round(float(prec), 4),
        "recall_at_3": round(float(rec), 4),
        "f1_at_3": round(float(f1), 4),
        "ndcg_at_10": round(float(np.mean(ndcgs)), 4),
        "sesiones_sin_positivos": no_pos,
        "sesiones_totales": int(df_e["sesion"].nunique())
    }

def main():
    df, cat = load_data()
    train_raw = df[df["dia"] <= 18].copy()
    val_raw = df[(df["dia"] >= 19) & (df["dia"] <= 23)].copy()
    test_raw = df[df["dia"] >= 24].copy()

    train, stats = build_features(train_raw)
    test, _ = build_features(test_raw, stats)
    cat_col = stats["cat_col"]

    features = ["contexto", cat_col, "precio_usd", "hora", "misma_cat", "u_ctr", "p_ctr"]
    pre = ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore"), ["contexto", cat_col]),
        ("num", "passthrough", ["precio_usd", "hora", "misma_cat", "u_ctr", "p_ctr"])
    ])
    clf = Pipeline([("pre", pre), ("lr", LogisticRegression(max_iter=1000, random_state=42))])
    clf.fit(train[features], train["clic"])

    test["score_base"] = test["p_ctr"]
    test["score_ml"] = clf.predict_proba(test[features])[:, 1]

    known_users = set(train["usuario"].unique())
    test["conocida"] = test["usuario"].isin(known_users)

    report = {
        "model_version": "r2-logreg-v1.0",
        "baseline_global": eval_ranking(test, "score_base"),
        "logreg_global": eval_ranking(test, "score_ml"),
        "logreg_conocidas": eval_ranking(test[test["conocida"]], "score_ml"),
        "logreg_nuevas": eval_ranking(test[~test["conocida"]], "score_ml")
    }

    artifact = {
        "model_version": "r2-logreg-v1.0",
        "schema_version": "1.0",
        "pipeline": clf,
        "stats": stats,
        "features": features,
        "catalog": cat.to_dict(orient="records")
    }
    art_path = os.path.join(MODEL_DIR, "model_r2.joblib")
    joblib.dump(artifact, art_path)
    sha = hashlib.sha256(open(art_path, "rb").read()).hexdigest()
    report["artifact_sha256"] = sha

    with open(os.path.join(MODEL_DIR, "metrics_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
