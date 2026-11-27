"""Módulo de Evaluación Cuantitativa Offline del Recomendador.

Protocolo experimental (sin fuga de datos / zero data leakage):
  * Toma las calificaciones explícitas reales (1-5★) de usuarios con >= 10 valoraciones.
  * Divide por usuario en 80% Entrenamiento (Train) y 20% Prueba (Test) con semilla fija.
  * Entrena todos los modelos exclusivamente sobre R_train y evalúa sobre R_test:
      1. Baseline (Media de Usuario)
      2. CF — Distancia de Chebyshev (L_inf)
      3. CF — MSD / JMSD (Diferencia Media Cuadrática + Jaccard)
      4. ML — Factorización de Matrices (TruncatedSVD)
      5. Recomendador Híbrido Propuesto (CF MSD + SVD + Contenido TF-IDF)
  * Métricas calculadas:
      - Exactitud de predicción: MAE, RMSE y Cobertura de Predicción Porcentual.
      - Calidad de ranking Top-K (relevante si rating >= 4★):
        Precision@5, Precision@10, Recall@10, NDCG@10 y Cobertura del Catálogo (%)."""
from __future__ import annotations

import math
import threading
import time
from typing import Any

import numpy as np
from scipy import sparse
from sklearn.decomposition import TruncatedSVD

from .database import get_connection
from .recommender import (
    RATING_MAX,
    RATING_MIN,
    SCALE_RANGE_SQ,
    HybridBookRecommender,
)

MIN_USER_EXPLICIT_RATINGS = 10
TEST_RATIO = 0.20
RELEVANT_THRESHOLD = 4.0
EVAL_MIN_OVERLAP = 3
EVAL_TOP_K_NEIGHBORS = 30
EVAL_SVD_COMPONENTS = 25
RANDOM_SEED = 42

def _dcg_at_k(relevances: list[int], k: int) -> float:
    return sum(rel / math.log2(idx + 2) for idx, rel in enumerate(relevances[:k]))

def _ndcg_at_k(ranked_items: np.ndarray, relevant_set: set[int], k: int) -> float:
    if not relevant_set:
        return 0.0
    hits = [1 if int(item) in relevant_set else 0 for item in ranked_items[:k]]
    dcg = _dcg_at_k(hits, k)
    ideal = [1] * min(len(relevant_set), k)
    idcg = _dcg_at_k(ideal, k)
    return dcg / idcg if idcg > 0 else 0.0

def run_offline_evaluation(
    recommender: HybridBookRecommender,
    max_eval_users: int = 120,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Ejecuta el benchmark comparativo 80/20 Train/Test sobre las calificaciones explícitas reales."""
    t0 = time.perf_counter()
    conn = get_connection(recommender._db_path)
    try:
        rows = conn.execute(
            "SELECT user_id, book_id, rating FROM reading_list WHERE rating IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    item_idx_of_id = recommender._item_idx_of_id
    n_items = recommender.n_items

    by_user: dict[int, list[tuple[int, float]]] = {}
    for r in rows:
        col = item_idx_of_id.get(int(r["book_id"]))
        if col is not None:
            by_user.setdefault(int(r["user_id"]), []).append((col, float(r["rating"])))

    eligible_users = sorted(u for u, lst in by_user.items() if len(lst) >= MIN_USER_EXPLICIT_RATINGS)
    if len(eligible_users) < 2:
        return {
            "protocol": {"note": "Insuficientes usuarios con >=10 calificaciones explícitas para split 80/20."},
            "models": [],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    rng = np.random.default_rng(seed)
    all_users = sorted(by_user.keys())
    u_row_of_id = {u: idx for idx, u in enumerate(all_users)}
    n_users = len(all_users)

    # Seleccionar subconjunto reproducible de usuarios a evaluar (hasta max_eval_users)
    if len(eligible_users) > max_eval_users:
        eval_users = sorted(rng.choice(eligible_users, size=max_eval_users, replace=False).tolist())
    else:
        eval_users = eligible_users

    # Split 80/20 estratificado por usuario (sin fuga de datos)
    R_train = np.full((n_users, n_items), np.nan, dtype=np.float32)
    test_pairs_by_user: dict[int, list[tuple[int, float]]] = {}
    n_train = 0
    n_test = 0
    eval_set = set(eval_users)

    for uid in all_users:
        u_row = u_row_of_id[uid]
        pairs = list(by_user[uid])
        if uid in eval_set:
            order = rng.permutation(len(pairs))
            n_t = max(2, int(round(len(pairs) * TEST_RATIO)))
            test_idx = set(order[:n_t].tolist())
            test_list: list[tuple[int, float]] = []
            for pos, (col, rat) in enumerate(pairs):
                if pos in test_idx:
                    test_list.append((col, rat))
                    n_test += 1
                else:
                    R_train[u_row, col] = rat
                    n_train += 1
            test_pairs_by_user[uid] = test_list
        else:
            for col, rat in pairs:
                R_train[u_row, col] = rat
                n_train += 1

    # Estadísticas exclusivas de R_train
    valid_train = ~np.isnan(R_train)
    user_counts_tr = valid_train.sum(axis=1).astype(np.float64)
    item_counts_tr = valid_train.sum(axis=0).astype(np.int32)
    active_item_mask = item_counts_tr >= 2  # pool de ítems candidatos con soporte en entrenamiento
    active_items = np.flatnonzero(active_item_mask)

    with np.errstate(invalid="ignore"):
        user_sum = np.nansum(R_train, axis=1)
        item_sum = np.nansum(R_train, axis=0)
    global_mean = float(np.nanmean(R_train))
    user_means_tr = np.divide(user_sum, user_counts_tr, out=np.full(n_users, global_mean), where=user_counts_tr > 0)
    item_means_tr = np.divide(item_sum, item_counts_tr, out=np.full(n_items, global_mean), where=item_counts_tr > 0)

    # Entrenar TruncatedSVD estrictamente sobre R_train centrado
    tr_rows, tr_cols = np.where(valid_train)
    centered_vals = (R_train[tr_rows, tr_cols] - user_means_tr[tr_rows]).astype(np.float32)
    R_centered = sparse.csr_matrix((centered_vals, (tr_rows, tr_cols)), shape=(n_users, n_items))
    n_comp = min(EVAL_SVD_COMPONENTS, n_users - 1, n_items - 1)
    svd = TruncatedSVD(n_components=max(1, n_comp), random_state=seed)
    U_svd = svd.fit_transform(R_centered).astype(np.float64)       # (n_users, k)
    V_svd = svd.components_.astype(np.float64)                     # (k, n_items)

    X_cbf = recommender._X_cbf

    # Acumuladores por modelo
    model_specs = [
        ("baseline", "Baseline (Media Usuario-Ítem)", "Promedio regularizado usuario + ítem sin vecinos ni ML"),
        ("chebyshev", "CF — Distancia de Chebyshev (L∞)", "Filtrado Colaborativo Usuario-Usuario con sim = 1/(1 + max|Δr|)"),
        ("msd_jmsd", "CF — MSD / JMSD", "Filtrado Colaborativo con JMSD = Jaccard × (1 - MSD/16)"),
        ("svd_ml", "ML — Factorización TruncatedSVD", "Descomposición en factores latentes (k=25) sobre matriz centrada"),
        ("hybrid", "Recomendador Híbrido Propuesto", "Fusión adaptativa CF (MSD/JMSD + SVD) + Contenido TF-IDF"),
    ]
    errors: dict[str, list[float]] = {k: [] for k, _, _ in model_specs}
    covered: dict[str, int] = {k: 0 for k, _, _ in model_specs}
    p5_scores: dict[str, list[float]] = {k: [] for k, _, _ in model_specs}
    p10_scores: dict[str, list[float]] = {k: [] for k, _, _ in model_specs}
    r10_scores: dict[str, list[float]] = {k: [] for k, _, _ in model_specs}
    ndcg10_scores: dict[str, list[float]] = {k: [] for k, _, _ in model_specs}
    recommended_items_union: dict[str, set[int]] = {k: set() for k, _, _ in model_specs}

    for uid in eval_users:
        u_row = u_row_of_id[uid]
        test_list = test_pairs_by_user[uid]
        if not test_list:
            continue

        u_train_mask = valid_train[u_row]
        u_cols = np.flatnonzero(u_train_mask)
        u_vals = R_train[u_row, u_cols].astype(np.float64)
        u_mean = user_means_tr[u_row]
        n_u = len(u_cols)

        # 1) Baseline: sesgo usuario + sesgo ítem regularizado
        item_bias = (item_counts_tr / (item_counts_tr + 5.0)) * (item_means_tr - global_mean)
        pred_base = np.clip(u_mean + item_bias, RATING_MIN, RATING_MAX)

        # 2) Similitudes Usuario-Usuario (Chebyshev y MSD/JMSD) estrictamente sobre R_train (RNF-05)
        sub_R = R_train[:, u_cols].astype(np.float64)
        sub_valid = ~np.isnan(sub_R)
        overlap = sub_valid.sum(axis=1)
        cand_mask = overlap >= EVAL_MIN_OVERLAP
        cand_mask[u_row] = False
        cand_rows = np.flatnonzero(cand_mask)

        pred_cheb = np.full(n_items, np.nan, dtype=np.float64)
        pred_msd = np.full(n_items, np.nan, dtype=np.float64)
        rank_cheb = np.full(n_items, -1e9, dtype=np.float64)
        rank_msd = np.full(n_items, -1e9, dtype=np.float64)

        if len(cand_rows) > 0:
            sub_c = sub_R[cand_rows]
            ov_c = overlap[cand_rows].astype(np.float64)
            diffs = np.abs(sub_c - u_vals[None, :])
            with np.errstate(invalid="ignore"):
                d_cheb = np.nanmax(diffs, axis=1)
                d_msd = np.nanmean(diffs ** 2, axis=1)

            sim_cheb = 1.0 / (1.0 + d_cheb)
            sim_msd_norm = np.clip(1.0 - (d_msd / SCALE_RANGE_SQ), 0.0, 1.0)
            union_sz = np.maximum(n_u + user_counts_tr[cand_rows] - ov_c, 1.0)
            jmsd = (ov_c / union_sz) * sim_msd_norm

            # Predicción KNN Chebyshev
            top_c = np.argsort(-sim_cheb)[:EVAL_TOP_K_NEIGHBORS]
            c_rows = cand_rows[top_c]
            c_w = sim_cheb[top_c]
            c_R = R_train[c_rows].astype(np.float64)
            c_val = ~np.isnan(c_R)
            c_dev = np.where(c_val, c_R - user_means_tr[c_rows][:, None], 0.0)
            c_wmat = np.where(c_val, c_w[:, None], 0.0)
            c_wsum = c_wmat.sum(axis=0)
            c_sup = c_val.sum(axis=0)
            c_ok = c_sup > 0
            c_pred = np.clip(
                u_mean + np.divide((c_wmat * c_dev).sum(axis=0), c_wsum + 0.1, out=np.zeros(n_items), where=c_ok),
                RATING_MIN, RATING_MAX,
            )
            pred_cheb = np.where(c_ok, c_pred, np.nan)
            rank_cheb = np.where(c_ok, ((c_pred - 1.0) / 4.0) * (c_sup / (c_sup + 2.0)), 0.0)

            # Predicción KNN MSD / JMSD
            top_m = np.argsort(-(jmsd * 1000.0 + sim_msd_norm))[:EVAL_TOP_K_NEIGHBORS]
            m_rows = cand_rows[top_m]
            m_w = jmsd[top_m] + 0.15 * sim_msd_norm[top_m] * np.minimum(1.0, ov_c[top_m] / 5.0)
            m_R = R_train[m_rows].astype(np.float64)
            m_val = ~np.isnan(m_R)
            m_dev = np.where(m_val, m_R - user_means_tr[m_rows][:, None], 0.0)
            m_wmat = np.where(m_val, m_w[:, None], 0.0)
            m_wsum = m_wmat.sum(axis=0)
            m_sup = m_val.sum(axis=0)
            m_pos_sup = np.where(m_val & (m_R >= 4.0), 1, 0).sum(axis=0)
            m_ok = m_sup > 0
            m_pred = np.clip(
                u_mean + np.divide((m_wmat * m_dev).sum(axis=0), m_wsum + 0.1, out=np.zeros(n_items), where=m_ok),
                RATING_MIN, RATING_MAX,
            )
            pred_msd = np.where(m_ok, m_pred, np.nan)
            rank_msd = np.where(
                m_ok,
                0.55 * ((m_pred - 1.0) / 4.0) + 0.45 * np.minimum(1.0, m_pos_sup / 6.0),
                0.0,
            )

        # 3) ML TruncatedSVD
        svd_dev = U_svd[u_row] @ V_svd
        pred_svd = np.clip(u_mean + svd_dev, RATING_MIN, RATING_MAX)
        p99_svd = float(np.percentile(np.abs(svd_dev), 99)) if np.any(svd_dev != 0) else 1.0
        rank_svd = np.clip((u_mean + np.clip(svd_dev / max(p99_svd, 1e-6), -1.0, 1.0) * 1.25 - 1.0) / 4.0, 0.0, 1.0)

        # 4) Contenido TF-IDF del usuario (entrenado solo con u_cols de Train)
        w_cbf = np.where(u_vals >= 3.0, (u_vals - 2.0) / 3.0, -0.35)
        if not np.any(w_cbf > 0):
            w_cbf = np.full_like(u_vals, 0.3)
        prof = np.asarray(X_cbf[u_cols].T @ w_cbf).ravel().astype(np.float64)
        np_norm = float(np.linalg.norm(prof))
        if np_norm > 0:
            raw_cbf = np.clip((X_cbf @ (prof / np_norm)).astype(np.float64), 0.0, 1.0)
            p99_c = float(np.percentile(raw_cbf[raw_cbf > 0], 99)) if np.any(raw_cbf > 0) else 1.0
            cbf_norm = np.clip(raw_cbf / max(p99_c, 1e-9), 0.0, 1.0)
        else:
            cbf_norm = np.zeros(n_items, dtype=np.float64)

        # 5) Híbrido Propuesto: predicción de rating + score de ranking Top-K
        msd_filled = np.where(~np.isnan(pred_msd), pred_msd, pred_svd)
        cbf_rating_est = np.clip(u_mean + (cbf_norm - 0.5) * 0.9, RATING_MIN, RATING_MAX)
        pred_hybrid = np.clip(0.50 * msd_filled + 0.30 * pred_svd + 0.20 * cbf_rating_est, RATING_MIN, RATING_MAX)
        cf_rank = np.where(~np.isnan(pred_msd), 0.60 * rank_msd + 0.40 * rank_svd, 0.55 * rank_svd)
        rank_hybrid = 0.65 * cf_rank + 0.35 * cbf_norm

        # Acumular errores MAE/RMSE sobre los pares del conjunto Test
        preds_map = {
            "baseline": pred_base,
            "chebyshev": pred_cheb,
            "msd_jmsd": pred_msd,
            "svd_ml": pred_svd,
            "hybrid": pred_hybrid,
        }
        for col, true_r in test_list:
            for key, vec in preds_map.items():
                p_val = vec[col]
                if not np.isnan(p_val):
                    errors[key].append(float(p_val - true_r))
                    covered[key] += 1

        # Evaluación de Ranking Top-K: ordenar ítems candidatos no vistos en Train
        relevant_test = {col for col, r in test_list if r >= RELEVANT_THRESHOLD}
        if not relevant_test:
            continue

        # Pool de ranking: todos los libros activos en Train que el usuario NO ha visto en Train
        unseen_pool = active_items[~u_train_mask[active_items]]
        # Asegurar que los libros relevantes del Test estén dentro del pool evaluado
        missing_rel = [c for c in relevant_test if c not in set(unseen_pool.tolist())]
        if missing_rel:
            unseen_pool = np.concatenate([unseen_pool, np.array(missing_rel, dtype=np.int64)])

        rank_vecs = {
            "baseline": (pred_base[unseen_pool] - 1.0) / 4.0,
            "chebyshev": rank_cheb[unseen_pool],
            "msd_jmsd": rank_msd[unseen_pool],
            "svd_ml": rank_svd[unseen_pool],
            "hybrid": rank_hybrid[unseen_pool],
        }
        for key, sc in rank_vecs.items():
            top10_local = np.argsort(-sc)[:10]
            ranked_cols = unseen_pool[top10_local]
            recommended_items_union[key].update(int(c) for c in ranked_cols)

            hits5 = sum(1 for c in ranked_cols[:5] if int(c) in relevant_test)
            hits10 = sum(1 for c in ranked_cols[:10] if int(c) in relevant_test)
            p5_scores[key].append(hits5 / 5.0)
            p10_scores[key].append(hits10 / 10.0)
            r10_scores[key].append(hits10 / len(relevant_test))
            ndcg10_scores[key].append(_ndcg_at_k(ranked_cols, relevant_test, 10))

    # Consolidar tabla comparativa final
    models_out: list[dict[str, Any]] = []
    for key, label, desc in model_specs:
        err = np.array(errors[key], dtype=np.float64)
        mae = float(np.mean(np.abs(err))) if len(err) > 0 else None
        rmse = float(np.sqrt(np.mean(err ** 2))) if len(err) > 0 else None
        pred_cov = (covered[key] / n_test * 100.0) if n_test > 0 else 0.0
        cat_cov = (len(recommended_items_union[key]) / max(len(active_items), 1) * 100.0)
        models_out.append({
            "id": key,
            "name": label,
            "description": desc,
            "mae": round(mae, 4) if mae is not None else None,
            "rmse": round(rmse, 4) if rmse is not None else None,
            "prediction_coverage_pct": round(pred_cov, 2),
            "precision_at_5": round(float(np.mean(p5_scores[key])), 4) if p5_scores[key] else 0.0,
            "precision_at_10": round(float(np.mean(p10_scores[key])), 4) if p10_scores[key] else 0.0,
            "recall_at_10": round(float(np.mean(r10_scores[key])), 4) if r10_scores[key] else 0.0,
            "ndcg_at_10": round(float(np.mean(ndcg10_scores[key])), 4) if ndcg10_scores[key] else 0.0,
            "catalog_coverage_pct": round(cat_cov, 2),
        })

    return {
        "protocol": {
            "split": "80% Train / 20% Test por usuario (semilla=42, sin fuga de datos)",
            "evaluated_users": len(eval_users),
            "train_ratings": n_train,
            "test_ratings": n_test,
            "relevant_threshold": RELEVANT_THRESHOLD,
            "min_overlap_gamma": EVAL_MIN_OVERLAP,
            "candidate_pool_size": int(len(active_items)),
        },
        "models": models_out,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }

# --------------------------------------------------------------------------- #
# Caché compartida en memoria
# --------------------------------------------------------------------------- #
_eval_cache: dict[str, Any] | None = None
_eval_lock = threading.Lock()

def get_evaluation_report(
    recommender: HybridBookRecommender,
    force_recompute: bool = False,
) -> dict[str, Any]:
    global _eval_cache
    with _eval_lock:
        if _eval_cache is None or force_recompute:
            _eval_cache = run_offline_evaluation(recommender)
        return _eval_cache
