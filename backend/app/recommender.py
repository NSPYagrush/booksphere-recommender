"""Motor de Recomendación Híbrido en 3 Niveles.

Arquitectura de 3 niveles:
  * Regla de votos efectivos: si existe calificación explícita (1-5★) prevalece;
    si es NULL se asigna un voto implícito temporal según estado de la lista de lectura:
        'por_leer' -> 3.0★  |  'leyendo' -> 3.7★  |  'leido' (sin nota) -> 4.0★
  * Nivel 1 — Basado en Contenido con ML (Content-Based):
        TfidfVectorizer sobre (géneros + autor + sinopsis) + Similitud del Coseno + afinidad de género.
  * Nivel 2 — Filtrado Colaborativo (Memoria + ML):
        - Memoria: Distancia de Chebyshev, MSD normalizado, MSD hiperbólico y JMSD strictly sobre la
          la intersección co-evaluada, sin imputar ceros en NaN, con umbral mínimo de superposición gamma >= 2.
        - Modelo ML: Factorización de matrices latentes con TruncatedSVD sobre la matriz centrada y
          proyección en vivo (fold-in) para usuarios nuevos o modificados.
  * Nivel 3 — Hibridación Adaptativa y Arranque en Frío:
        - Cold Start (0 libros en lista): 0.65 * AfinidadGénero_CBF + 0.35 * PopularidadBayesiana.
        - Usuario Activo (>=1 libro): alpha * Score_CF + (1 - alpha) * Score_CBF, con alpha adaptativo.

Salidas públicas para la aplicación:
  1. recommend_for_home(user_id)            -> "Recomendados para ti" (Inicio)
  2. recommend_by_genre(user_id, genre)     -> "Porque te interesa el género [X]" (Pestaña Géneros)
  3. recommend_similar_books(book_id)       -> "Lectores similares también agregaron..." (Detalle)
  + compute_user_neighbors(user_id)         -> Vecindario MSD/JMSD
  + compute_item_similarities(book_id)      -> Métricas Ítem-Ítem exactas"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer

from .database import get_connection
from .search_engine import normalize_text

# --------------------------------------------------------------------------- #
# Constantes de negocio e hiperparámetros
# --------------------------------------------------------------------------- #
IMPLICIT_RATING_BY_STATUS: dict[str, float] = {
    "por_leer": 3.0,
    "leyendo": 3.7,
    "leido": 4.0,
}

RATING_MIN, RATING_MAX = 1.0, 5.0
SCALE_RANGE_SQ = (RATING_MAX - RATING_MIN) ** 2  # (5 - 1)^2 = 16.0

# Pesos de hibridación
COLD_W_CBF = 0.65
COLD_W_POP = 0.35
CF_W_MSD = 0.60
CF_W_SVD = 0.40
ITEM_W_CF = 0.65
ITEM_W_CBF = 0.35

# Umbrales de filtrado colaborativo (nunca permitir min_overlap < 2)
DEFAULT_USER_MIN_OVERLAP = 3
DEFAULT_ITEM_MIN_OVERLAP = 5
MIN_ALLOWED_OVERLAP = 2
USER_NEIGHBOR_K = 35
SVD_COMPONENTS = 30
SNIPPET_CHARS = 220

def effective_rating(status: str, rating: float | None) -> float:
    """Devuelve la calificación efectiva r*_(u,i) aplicando votos implícitos temporales."""
    if rating is not None and not pd.isna(rating):
        return float(np.clip(float(rating), RATING_MIN, RATING_MAX))
    return IMPLICIT_RATING_BY_STATUS.get(status, 3.0)

# --------------------------------------------------------------------------- #
# Motor Híbrido
# --------------------------------------------------------------------------- #
class HybridBookRecommender:
    """Recomendador Híbrido en 3 Niveles con actualización reactiva desde SQLite."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = db_path
        self._state_lock = threading.RLock()
        self.reload()

    # build
    def reload(self) -> None:
        """Carga el catálogo y la matriz inicial desde SQLite y ajusta TF-IDF + TruncatedSVD."""
        with self._state_lock:
            conn = get_connection(self._db_path)
            try:
                books_df = pd.read_sql_query(
                    "SELECT book_id, title, author, genres, primary_genre, synopsis, "
                    "publish_year, pages, isbn, avg_rating, ratings_count "
                    "FROM books WHERE COALESCE(in_recommender, 1) = 1 ORDER BY book_id",
                    conn,
                )
                rl_df = pd.read_sql_query(
                    "SELECT user_id, book_id, status, rating FROM reading_list",
                    conn,
                )
            finally:
                conn.close()

            if books_df.empty:
                raise RuntimeError("La tabla 'books' está vacía: ejecute primero 'python -m app.etl_seed --reset'.")

            books_df["synopsis"] = books_df["synopsis"].fillna("")
            self.books_df = books_df
            self.n_items = len(books_df)
            self.book_ids = books_df["book_id"].to_numpy(dtype=np.int64)
            self._item_idx_of_id = {int(b): i for i, b in enumerate(self.book_ids)}

            # Metadatos de géneros y popularidad bayesiana
            self._genre_lists: list[list[str]] = [
                [g.strip() for g in str(s).split(",") if g.strip()] for s in books_df["genres"]
            ]
            self._genre_sets: list[set[str]] = [set(gl) for gl in self._genre_lists]
            self._genre_masks: dict[str, np.ndarray] = {}
            for i, gl in enumerate(self._genre_lists):
                for g in gl:
                    self._genre_masks.setdefault(g, np.zeros(self.n_items, dtype=bool))[i] = True
            self._genre_lookup = {normalize_text(g): g for g in self._genre_masks}

            counts = books_df["ratings_count"].to_numpy(dtype=np.float64)
            ratings = books_df["avg_rating"].to_numpy(dtype=np.float64)
            rated_mask = (~np.isnan(ratings)) & (counts > 0)
            self.prior_c = float(np.mean(ratings[rated_mask])) if np.any(rated_mask) else 3.5
            self.prior_m = max(
                float(np.quantile(counts[rated_mask], 0.25)) if np.any(rated_mask) else 10.0,
                1.0,
            )
            weighted_r = np.zeros(self.n_items, dtype=np.float64)
            if np.any(rated_mask):
                v = counts[rated_mask]
                r_val = ratings[rated_mask]
                weighted_r[rated_mask] = (v / (v + self.prior_m)) * r_val + (self.prior_m / (v + self.prior_m)) * self.prior_c
            self.weighted_ratings = weighted_r

            # Puntaje normalizado de popularidad bayesiana en [0, 1] (neutro=0.15 si el libro no trae rating externo)
            if np.any(rated_mask):
                wr_min = float(weighted_r[rated_mask].min())
                wr_max = float(weighted_r[rated_mask].max())
                wr_norm = np.where(rated_mask, (weighted_r - wr_min) / max(wr_max - wr_min, 1e-9), 0.0)
                log_pop = np.log1p(np.maximum(counts, 0.0))
                log_pop_norm = log_pop / max(float(log_pop.max()), 1e-9)
                self.pop_score = np.where(rated_mask, 0.65 * wr_norm + 0.35 * log_pop_norm, 0.15)
            else:
                self.pop_score = np.full(self.n_items, 0.15, dtype=np.float64)

            # Nivel 1: Matriz TF-IDF de contenido (L2-normalizada)
            self._build_content_model(books_df)

            # Nivel 2: Matriz Usuario-Ítem (NaN en celdas vacías) + TruncatedSVD
            self._build_collaborative_model(rl_df)

    def _build_content_model(self, books_df: pd.DataFrame) -> None:
        """Nivel 1: TF-IDF semántico ponderando géneros (x3), género principal (x2), autor (x2) y sinopsis."""
        corpus = [
            f"{(r.genres + ' ') * 3}{(r.primary_genre + ' ') * 2}{(r.author + ' ') * 2}{r.synopsis}"
            for r in books_df.itertuples(index=False)
        ]
        self._cbf_vec = TfidfVectorizer(
            max_features=8000,
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True,
            dtype=np.float32,
        )
        self._X_cbf = self._cbf_vec.fit_transform(corpus).tocsr()

    def _build_collaborative_model(self, rl_df: pd.DataFrame) -> None:
        """Construye la matriz de calificaciones efectivas (NaN en no observados) y entrena TruncatedSVD."""
        if rl_df.empty:
            self.user_ids = np.array([], dtype=np.int64)
            self._user_idx_of_id: dict[int, int] = {}
            self._R = np.empty((0, self.n_items), dtype=np.float32)
            self._user_means = np.array([], dtype=np.float64)
            self._user_counts = np.array([], dtype=np.int32)
            self._item_counts = np.zeros(self.n_items, dtype=np.int32)
            self._svd_V = np.zeros((1, self.n_items), dtype=np.float32)
            return

        rl = rl_df.copy()
        rl["eff"] = [effective_rating(s, r) for s, r in zip(rl["status"], rl["rating"])]
        rl["col"] = rl["book_id"].map(self._item_idx_of_id)
        rl = rl.dropna(subset=["col"]).copy()
        rl["col"] = rl["col"].astype(np.int64)

        unique_users = np.sort(rl["user_id"].unique().astype(np.int64))
        self.user_ids = unique_users
        self._user_idx_of_id = {int(u): idx for idx, u in enumerate(unique_users)}
        n_users = len(unique_users)

        # Matriz densa float32 con NaN en no observados
        R = np.full((n_users, self.n_items), np.nan, dtype=np.float32)
        rows_idx = rl["user_id"].map(self._user_idx_of_id).to_numpy(dtype=np.int64)
        cols_idx = rl["col"].to_numpy(dtype=np.int64)
        vals = rl["eff"].to_numpy(dtype=np.float32)
        R[rows_idx, cols_idx] = vals

        self._R = R
        self._recompute_matrix_stats()
        self._fit_svd()

    def _recompute_matrix_stats(self) -> None:
        valid = ~np.isnan(self._R)
        self._user_counts = valid.sum(axis=1).astype(np.int32)
        self._item_counts = valid.sum(axis=0).astype(np.int32)
        with np.errstate(invalid="ignore"):
            means = np.nanmean(self._R, axis=1)
        self._user_means = np.where(np.isnan(means), self.prior_c, means).astype(np.float64)

    def _fit_svd(self) -> None:
        """Ajusta TruncatedSVD sobre la matriz dispersa de desviaciones centradas por la media de cada usuario."""
        n_users = self._R.shape[0]
        if n_users < 2 or self.n_items < 2:
            self._svd_V = np.zeros((1, self.n_items), dtype=np.float32)
            return

        rows, cols = np.where(~np.isnan(self._R))
        if len(rows) == 0:
            self._svd_V = np.zeros((1, self.n_items), dtype=np.float32)
            return

        # Centrado por usuario: los no observados quedan en 0 SOLO como desviación nula respecto a la media
        centered_vals = (self._R[rows, cols] - self._user_means[rows]).astype(np.float32)
        R_centered = sparse.csr_matrix((centered_vals, (rows, cols)), shape=(n_users, self.n_items))

        n_comp = min(SVD_COMPONENTS, n_users - 1, self.n_items - 1)
        if n_comp < 1:
            self._svd_V = np.zeros((1, self.n_items), dtype=np.float32)
            return

        svd = TruncatedSVD(n_components=n_comp, random_state=42)
        svd.fit(R_centered)
        self._svd_V = svd.components_.astype(np.float32)  # shape (k, n_items)

    # reactividad en vivo
    def _load_live_user(self, user_id: int) -> tuple[list[str], dict[int, dict[str, Any]]]:
        """Lee directamente de SQLite los géneros favoritos y la lista de lectura actual del usuario."""
        conn = get_connection(self._db_path)
        try:
            u_row = conn.execute(
                "SELECT user_id, favorite_genres FROM users WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
            if u_row is None:
                raise ValueError(f"Usuario {user_id} no encontrado.")

            fav_genres = [g.strip() for g in str(u_row["favorite_genres"] or "").split(",") if g.strip()]
            rows = conn.execute(
                "SELECT book_id, status, rating FROM reading_list WHERE user_id = ?",
                (int(user_id),),
            ).fetchall()
        finally:
            conn.close()

        interactions: dict[int, dict[str, Any]] = {}
        for row in rows:
            bid = int(row["book_id"])
            if bid in self._item_idx_of_id:
                interactions[bid] = {
                    "item_idx": self._item_idx_of_id[bid],
                    "status": row["status"],
                    "explicit_rating": None if row["rating"] is None else float(row["rating"]),
                    "effective_rating": effective_rating(row["status"], row["rating"]),
                }
        return fav_genres, interactions

    def get_user_effective_ratings(self, user_id: int) -> dict[int, float]:
        """Devuelve {book_id: r*_(u,i)} con la regla de votos implícitos temporales aplicada."""
        _, interactions = self._load_live_user(user_id)
        return {bid: info["effective_rating"] for bid, info in interactions.items()}

    def sync_user_row(self, user_id: int) -> None:
        """Actualiza la fila del usuario en la matriz en memoria tras una edición en su lista."""
        with self._state_lock:
            _, interactions = self._load_live_user(user_id)
            uid = int(user_id)
            row_vec = np.full(self.n_items, np.nan, dtype=np.float32)
            for info in interactions.values():
                row_vec[info["item_idx"]] = np.float32(info["effective_rating"])

            if uid in self._user_idx_of_id:
                u_idx = self._user_idx_of_id[uid]
                self._R[u_idx, :] = row_vec
            else:
                new_idx = len(self.user_ids)
                self.user_ids = np.append(self.user_ids, np.int64(uid))
                self._user_idx_of_id[uid] = new_idx
                self._R = np.vstack([self._R, row_vec[None, :]]) if self._R.size else row_vec[None, :]
            self._recompute_matrix_stats()

    # Nivel 1: Content-Based
    def _compute_cbf_scores(
        self,
        fav_genres: list[str],
        interactions: dict[int, dict[str, Any]],
    ) -> tuple[np.ndarray, dict[int, str]]:
        """Calcula Score_CBF(u, i) in [0, 1] y el libro ancla más similar de la lista del usuario."""
        fav_canon = [
            self._genre_lookup[normalize_text(g)]
            for g in fav_genres
            if normalize_text(g) in self._genre_lookup
        ]

        # Caso A: Cold Start (0 libros en la lista) -> perfil construido con géneros favoritos
        if not interactions:
            if not fav_canon:
                return np.zeros(self.n_items, dtype=np.float64), {}

            query_text = " ".join(fav_canon * 3)
            q_vec = self._cbf_vec.transform([query_text])
            raw_cos = (self._X_cbf @ q_vec.T).toarray().ravel().astype(np.float64)
            scale = float(np.percentile(raw_cos[raw_cos > 0], 99)) if np.any(raw_cos > 0) else 1.0
            cos_norm = np.clip(raw_cos / max(scale, 1e-9), 0.0, 1.0)

            # Afinidad directa por pertenencia a los géneros elegidos en el registro
            match_count = np.zeros(self.n_items, dtype=np.float64)
            primary_bonus = np.zeros(self.n_items, dtype=np.float64)
            fav_set = set(fav_canon)
            for g in fav_canon:
                match_count += self._genre_masks[g].astype(np.float64)
            for i, pg in enumerate(self.books_df["primary_genre"]):
                if pg in fav_set:
                    primary_bonus[i] = 1.0
            genre_aff = np.clip(0.65 * (match_count > 0).astype(np.float64)
                                + 0.20 * (match_count / max(len(fav_canon), 1))
                                + 0.15 * primary_bonus, 0.0, 1.0)
            return 0.45 * cos_norm + 0.55 * genre_aff, {}

        # Caso B: Usuario activo -> vector ponderado por sus libros en lista + géneros aprendidos
        item_indices = np.array([v["item_idx"] for v in interactions.values()], dtype=np.int64)
        eff_ratings = np.array([v["effective_rating"] for v in interactions.values()], dtype=np.float64)

        # Pesos: >= 3.0 positivos [0.33..1.0], < 3.0 retroalimentación negativa suave [-0.50..-0.25]
        weights = np.where(
            eff_ratings >= 3.0,
            (eff_ratings - 2.0) / 3.0,
            np.where(eff_ratings <= 1.5, -0.50, -0.25),
        )
        # Si todos los libros fueron mal calificados, mantenemos pesos positivos mínimos para no anular perfil
        if not np.any(weights > 0):
            weights = np.full_like(eff_ratings, 0.3)

        sub_X = self._X_cbf[item_indices]
        profile = np.asarray(sub_X.T @ weights).ravel().astype(np.float64)

        # Refuerzo suave de géneros favoritos declarados
        if fav_canon:
            fav_vec = self._cbf_vec.transform([" ".join(fav_canon * 2)]).toarray().ravel().astype(np.float64)
            fav_weight = 0.35 if len(interactions) < 5 else 0.15
            norm_p = float(np.linalg.norm(profile))
            norm_f = float(np.linalg.norm(fav_vec))
            if norm_p > 0 and norm_f > 0:
                profile = (profile / norm_p) + fav_weight * (fav_vec / norm_f)

        norm_p = float(np.linalg.norm(profile))
        if norm_p > 0:
            profile /= norm_p
            raw_cos = (self._X_cbf @ profile).astype(np.float64)
        else:
            raw_cos = np.zeros(self.n_items, dtype=np.float64)

        raw_cos = np.clip(raw_cos, 0.0, 1.0)
        pos_mask = raw_cos > 0
        scale = float(np.percentile(raw_cos[pos_mask], 99)) if np.any(pos_mask) else 1.0
        cos_norm = np.clip(raw_cos / max(scale, 1e-9), 0.0, 1.0)

        # Afinidad de género aprendida desde los libros positivos en la lista del usuario
        genre_weights: dict[str, float] = {}
        for idx, r_eff in zip(item_indices, eff_ratings):
            if r_eff < 3.0:
                continue
            w = (r_eff - 2.0) / 3.0
            for g in self._genre_lists[idx]:
                if g not in ("Fiction", "Nonfiction"):
                    genre_weights[g] = genre_weights.get(g, 0.0) + w
        for g in fav_canon:
            genre_weights[g] = genre_weights.get(g, 0.0) + 0.5

        genre_aff = np.zeros(self.n_items, dtype=np.float64)
        if genre_weights:
            max_gw = max(genre_weights.values())
            for g, gw in genre_weights.items():
                if g in self._genre_masks:
                    genre_aff += (gw / max_gw) * self._genre_masks[g].astype(np.float64)
            if genre_aff.max() > 0:
                genre_aff = np.clip(genre_aff / np.percentile(genre_aff[genre_aff > 0], 98), 0.0, 1.0)

        cbf_score = 0.70 * cos_norm + 0.30 * genre_aff

        # Libro ancla más similar de la lista del usuario
        pos_sel = np.where(eff_ratings >= 3.0)[0]
        anchor_map: dict[int, str] = {}
        if len(pos_sel) > 0:
            # Top 15 libros mejor valorados de la lista del usuario como anclas explicativas
            top_pos = pos_sel[np.argsort(-eff_ratings[pos_sel])[:15]]
            anchor_indices = item_indices[top_pos]
            sims_to_anchors = (self._X_cbf @ self._X_cbf[anchor_indices].T).toarray()
            best_col = np.argmax(sims_to_anchors, axis=1)
            best_sim = sims_to_anchors[np.arange(self.n_items), best_col]
            anchor_titles = [str(self.books_df.iloc[i]["title"]) for i in anchor_indices]
            for item_i in np.flatnonzero(best_sim >= 0.08):
                anchor_map[int(item_i)] = anchor_titles[int(best_col[item_i])]

        return cbf_score, anchor_map

    # Nivel 2: Colaborativo MSD+SVD
    def compute_user_neighbors(
        self,
        user_id: int,
        min_overlap: int = DEFAULT_USER_MIN_OVERLAP,
        top_k: int = USER_NEIGHBOR_K,
    ) -> list[dict[str, Any]]:
        """Calcula vecinos Usuario-Usuario con Chebyshev, MSD normalizado/hiperbólico y JMSD ."""
        min_ov = max(int(min_overlap), MIN_ALLOWED_OVERLAP)
        _, interactions = self._load_live_user(user_id)
        return self._compute_user_neighbors_from_interactions(user_id, interactions, min_ov, top_k)

    def _compute_user_neighbors_from_interactions(
        self,
        user_id: int,
        interactions: dict[int, dict[str, Any]],
        min_overlap: int,
        top_k: int,
    ) -> list[dict[str, Any]]:
        if len(interactions) < min_overlap or self._R.shape[0] == 0:
            return []

        cols = np.array([v["item_idx"] for v in interactions.values()], dtype=np.int64)
        u_vals = np.array([v["effective_rating"] for v in interactions.values()], dtype=np.float64)
        n_u = len(cols)

        # Cortar solo las columnas evaluadas por el usuario objetivo (nunca imputar ceros en NaN)
        sub_R = self._R[:, cols].astype(np.float64)
        valid = ~np.isnan(sub_R)
        overlap = valid.sum(axis=1)

        # Excluir al propio usuario y exigir |I_uv| >= min_overlap
        cand_mask = overlap >= min_overlap
        self_idx = self._user_idx_of_id.get(int(user_id))
        if self_idx is not None:
            cand_mask[self_idx] = False

        cand_rows = np.flatnonzero(cand_mask)
        if len(cand_rows) == 0:
            return []

        sub_cand = sub_R[cand_rows]
        ov_cand = overlap[cand_rows].astype(np.float64)
        diffs = np.abs(sub_cand - u_vals[None, :])

        # Métricas exactas sobre la intersección co-evaluada I_uv
        with np.errstate(invalid="ignore"):
            d_cheb = np.nanmax(diffs, axis=1)
            d_msd = np.nanmean(diffs ** 2, axis=1)

        sim_cheb = 1.0 / (1.0 + d_cheb)
        sim_msd_hyp = 1.0 / (1.0 + d_msd)
        sim_msd_norm = np.clip(1.0 - (d_msd / SCALE_RANGE_SQ), 0.0, 1.0)
        union_size = np.maximum(n_u + self._user_counts[cand_rows] - ov_cand, 1.0)
        jaccard = ov_cand / union_size
        jmsd = jaccard * sim_msd_norm

        # Ordenar por JMSD
        order = np.argsort(-(jmsd * 1000.0 + sim_msd_norm))[:top_k]

        neighbors: list[dict[str, Any]] = []
        for pos in order:
            r_idx = int(cand_rows[pos])
            neighbors.append({
                "row_idx": r_idx,
                "user_id": int(self.user_ids[r_idx]),
                "overlap_books": int(ov_cand[pos]),
                "chebyshev_dist": round(float(d_cheb[pos]), 4),
                "sim_chebyshev": round(float(sim_cheb[pos]), 4),
                "msd_value": round(float(d_msd[pos]), 4),
                "sim_msd_hyp": round(float(sim_msd_hyp[pos]), 4),
                "sim_msd_norm": round(float(sim_msd_norm[pos]), 4),
                "jaccard": round(float(jaccard[pos]), 4),
                "jmsd": round(float(jmsd[pos]), 4),
                "affinity_percent": round(float(sim_msd_norm[pos]) * 100.0, 1),
            })
        return neighbors

    def _compute_cf_scores(
        self,
        user_id: int,
        interactions: dict[int, dict[str, Any]],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
        """Devuelve (cf_score, msd_score, svd_score, neighbor_support, n_neighbors)."""
        zeros = np.zeros(self.n_items, dtype=np.float64)
        zero_sup = np.zeros(self.n_items, dtype=np.int32)
        if not interactions or self._R.shape[0] == 0:
            return zeros, zeros, zeros, zero_sup, 0

        cols = np.array([v["item_idx"] for v in interactions.values()], dtype=np.int64)
        u_vals = np.array([v["effective_rating"] for v in interactions.values()], dtype=np.float64)
        u_mean = float(np.mean(u_vals))

        # 1) Submódulo de Memoria: predicción centrada en la media con vecinos MSD/JMSD
        min_ov = DEFAULT_USER_MIN_OVERLAP if len(interactions) >= 5 else MIN_ALLOWED_OVERLAP
        neighbors = self._compute_user_neighbors_from_interactions(user_id, interactions, min_ov, USER_NEIGHBOR_K)
        if not neighbors and min_ov > MIN_ALLOWED_OVERLAP:
            neighbors = self._compute_user_neighbors_from_interactions(
                user_id, interactions, MIN_ALLOWED_OVERLAP, USER_NEIGHBOR_K
            )

        msd_score = np.zeros(self.n_items, dtype=np.float64)
        msd_has_signal = np.zeros(self.n_items, dtype=bool)
        neighbor_support = np.zeros(self.n_items, dtype=np.int32)

        if neighbors:
            nb_rows = np.array([n["row_idx"] for n in neighbors], dtype=np.int64)
            # Peso por vecino: JMSD con piso suave de sim_msd_norm ponderado por solapamiento
            nb_weights = np.array(
                [n["jmsd"] + 0.15 * n["sim_msd_norm"] * min(1.0, n["overlap_books"] / 5.0) for n in neighbors],
                dtype=np.float64,
            )
            nb_R = self._R[nb_rows].astype(np.float64)  # (K, n_items) con NaN en no observados
            nb_valid = ~np.isnan(nb_R)
            nb_means = self._user_means[nb_rows][:, None]

            # Desviaciones centradas (r_vi - mean_v) solo donde nb_valid es True
            nb_dev = np.where(nb_valid, nb_R - nb_means, 0.0)
            w_mat = np.where(nb_valid, nb_weights[:, None], 0.0)
            w_sum = w_mat.sum(axis=0)
            sup_count = nb_valid.sum(axis=0).astype(np.int32)
            # Vecinos que valoraron positivamente (>= 4★) cada libro candidato
            neighbor_support = np.where(nb_valid & (nb_R >= 4.0), 1, 0).sum(axis=0).astype(np.int32)

            msd_has_signal = sup_count > 0
            pred_dev = np.divide(
                (w_mat * nb_dev).sum(axis=0),
                w_sum + 0.10,
                out=np.zeros(self.n_items, dtype=np.float64),
                where=msd_has_signal,
            )
            # Suavizado por soporte: un libro respaldado por muchos vecinos MSD pesa más que 1 solo voto
            confidence = sup_count / (sup_count + 2.0)
            pred_rating = np.clip(u_mean + pred_dev * confidence, RATING_MIN, RATING_MAX)
            # Bonus ligero por consenso positivo dentro del vecindario
            consensus_bonus = np.clip(neighbor_support / max(len(neighbors) * 0.25, 3.0), 0.0, 1.0)
            msd_score = np.where(
                msd_has_signal,
                0.75 * ((pred_rating - RATING_MIN) / (RATING_MAX - RATING_MIN)) + 0.25 * consensus_bonus,
                0.0,
            )

        # 2) Submódulo ML: proyección en vivo (fold-in) en espacio latente TruncatedSVD
        svd_score = np.zeros(self.n_items, dtype=np.float64)
        if self._svd_V.shape[0] > 0 and self._svd_V.shape[1] == self.n_items:
            # Vector de desviaciones centradas del usuario (si todas sus calificaciones son iguales, usa u_vals - prior_c)
            dev_u = u_vals - u_mean
            if np.allclose(dev_u, 0.0):
                dev_u = u_vals - self.prior_c
            z_u = dev_u @ self._svd_V[:, cols].T  # (k,)
            raw_svd_dev = (z_u @ self._svd_V).astype(np.float64)
            # Calibrar amplitud latente sobre los libros con interacción para alinear con escala [1, 5]
            p99 = float(np.percentile(np.abs(raw_svd_dev), 99)) if np.any(raw_svd_dev != 0) else 1.0
            scaled_dev = np.clip(raw_svd_dev / max(p99, 1e-6), -1.0, 1.0)
            svd_rating = np.clip(u_mean + scaled_dev * 1.25, RATING_MIN, RATING_MAX)
            svd_score = (svd_rating - RATING_MIN) / (RATING_MAX - RATING_MIN)
            # Solo donde SVD tiene proyección no nula
            svd_score = np.where(self._item_counts > 0, svd_score, 0.0)

        # 3) Fusión Colaborativa (MSD + SVD)
        cf_score = np.where(
            msd_has_signal,
            CF_W_MSD * msd_score + CF_W_SVD * svd_score,
            0.55 * svd_score,  # ítems sin vecinos MSD directos pero con factor latente SVD
        )
        return cf_score, msd_score, svd_score, neighbor_support, len(neighbors)

    # Nivel 3: Hibridación y salidas
    def _score_all_items_for_user(self, user_id: int) -> dict[str, Any]:
        """Ejecuta los 3 niveles para un usuario y devuelve los vectores de puntaje y metadatos."""
        with self._state_lock:
            fav_genres, interactions = self._load_live_user(user_id)
            n_list = len(interactions)
            seen_indices = {v["item_idx"] for v in interactions.values()}
            unseen_mask = np.ones(self.n_items, dtype=bool)
            if seen_indices:
                unseen_mask[list(seen_indices)] = False

            cbf_score, anchor_map = self._compute_cbf_scores(fav_genres, interactions)

            if n_list == 0:
                # Arranque en Frío
                final_score = COLD_W_CBF * cbf_score + COLD_W_POP * self.pop_score
                return {
                    "mode": "cold_start",
                    "alpha": 0.0,
                    "fav_genres": fav_genres,
                    "n_list": 0,
                    "n_neighbors": 0,
                    "unseen_mask": unseen_mask,
                    "final_score": final_score,
                    "cf_score": np.zeros(self.n_items, dtype=np.float64),
                    "msd_score": np.zeros(self.n_items, dtype=np.float64),
                    "svd_score": np.zeros(self.n_items, dtype=np.float64),
                    "cbf_score": cbf_score,
                    "neighbor_support": np.zeros(self.n_items, dtype=np.int32),
                    "anchor_map": anchor_map,
                }

            # Usuario Activo: Hibridación Adaptativa
            cf_score, msd_score, svd_score, neighbor_support, n_neighbors = self._compute_cf_scores(
                user_id, interactions
            )
            # alpha crece con la actividad del usuario [0.25 .. 0.65] (reducido si aún no hay vecinos MSD)
            alpha = min(0.65, 0.20 + 0.05 * n_list)
            if n_neighbors == 0:
                alpha = min(alpha, 0.25)

            # Fórmula híbrida ponderada: los ítems sin señal colaborativa (cola larga sin ratings)
            # reciben un valor base neutro conservador (0.35) en CF para no superar injustamente
            # a libros respaldados tanto por el vecindario MSD/SVD como por el contenido TF-IDF.
            has_cf = cf_score > 0
            cf_effective = np.where(has_cf, cf_score, 0.35)
            hybrid = alpha * cf_effective + (1.0 - alpha) * cbf_score
            # Suavizado ligero de calidad bayesiana para desempatar hacia libros bien valorados
            final_score = 0.95 * hybrid + 0.05 * self.pop_score

            return {
                "mode": "hybrid",
                "alpha": round(float(alpha), 3),
                "fav_genres": fav_genres,
                "n_list": n_list,
                "n_neighbors": n_neighbors,
                "unseen_mask": unseen_mask,
                "final_score": final_score,
                "cf_score": cf_score,
                "msd_score": msd_score,
                "svd_score": svd_score,
                "cbf_score": cbf_score,
                "neighbor_support": neighbor_support,
                "anchor_map": anchor_map,
            }

    def _format_book_recommendation(
        self,
        item_idx: int,
        ctx: dict[str, Any],
        forced_genre: str | None = None,
    ) -> dict[str, Any]:
        r = self.books_df.iloc[item_idx]
        synopsis = str(r["synopsis"])
        snippet = (
            synopsis if len(synopsis) <= SNIPPET_CHARS
            else synopsis[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"
        )
        score = float(ctx["final_score"][item_idx])
        cf_val = float(ctx["cf_score"][item_idx])
        msd_val = float(ctx["msd_score"][item_idx])
        svd_val = float(ctx["svd_score"][item_idx])
        cbf_val = float(ctx["cbf_score"][item_idx])
        pop_val = float(self.pop_score[item_idx])
        nb_sup = int(ctx["neighbor_support"][item_idx])
        anchor = ctx["anchor_map"].get(item_idx)
        genres_list = self._genre_lists[item_idx]

        # Explicabilidad
        if ctx["mode"] == "cold_start":
            matching_favs = [g for g in ctx["fav_genres"] if g in self._genre_sets[item_idx]]
            genre_label = forced_genre or (matching_favs[0] if matching_favs else str(r["primary_genre"]))
            rating_txt = f"{float(r['avg_rating']):.2f}★" if pd.notna(r["avg_rating"]) else ""
            reason = f"Recomendado por tu interés en {genre_label}" + (
                f" ({rating_txt} en la comunidad)" if rating_txt else ""
            )
        else:
            parts: list[str] = []
            if forced_genre:
                parts.append(f"Género {forced_genre}")
            if nb_sup >= 2:
                parts.append(f"{nb_sup} lectores de tu vecindario lo valoraron alto")
            elif cf_val > 0.35:
                parts.append("alta afinidad colaborativa")
            if anchor:
                parts.append(f"similar a «{anchor}» en tu lista")
            elif genres_list:
                parts.append(f"afín a tus lecturas de {genres_list[0]}")
            reason = " · ".join(parts) if parts else "Recomendación personalizada"

        return {
            "book_id": int(r["book_id"]),
            "title": str(r["title"]),
            "author": str(r["author"]),
            "genres": genres_list,
            "primary_genre": str(r["primary_genre"]),
            "publish_year": None if pd.isna(r["publish_year"]) else int(r["publish_year"]),
            "pages": None if pd.isna(r["pages"]) else int(r["pages"]),
            "avg_rating": None if pd.isna(r["avg_rating"]) else float(r["avg_rating"]),
            "ratings_count": int(r["ratings_count"]),
            "synopsis_snippet": snippet,
            "recommendation": {
                "score": round(score, 4),
                "match_percent": int(round(np.clip(score, 0.0, 1.0) * 100)),
                "mode": ctx["mode"],
                "alpha": ctx["alpha"],
                "neighbor_endorsements": nb_sup,
                "components": {
                    "cf": round(cf_val, 4),
                    "msd": round(msd_val, 4),
                    "svd": round(svd_val, 4),
                    "content": round(cbf_val, 4),
                    "popularity": round(pop_val, 4),
                },
                "reason": reason,
            },
        }

    # Salida 1: Inicio ("Recomendados para ti")
    def recommend_for_home(self, user_id: int, top_n: int = 15) -> dict[str, Any]:
        """Salida 1: Recomendaciones principales personalizadas en Inicio."""
        if not 1 <= top_n <= 100:
            raise ValueError("top_n debe estar entre 1 y 100")

        ctx = self._score_all_items_for_user(user_id)
        cand_idx = np.flatnonzero(ctx["unseen_mask"])
        scores = ctx["final_score"][cand_idx]
        order = np.lexsort((self.book_ids[cand_idx], -self.pop_score[cand_idx], -scores))
        chosen = cand_idx[order][:top_n]

        return {
            "user_id": int(user_id),
            "mode": ctx["mode"],
            "alpha": ctx["alpha"],
            "books_in_list": ctx["n_list"],
            "active_neighbors": ctx["n_neighbors"],
            "favorite_genres": ctx["fav_genres"],
            "recommendations": [self._format_book_recommendation(int(i), ctx) for i in chosen],
        }

    # Salida 2: Pestaña Exclusiva de Géneros ("Porque te interesa [X]")
    def recommend_by_genre(self, user_id: int, genre: str, top_n: int = 10) -> dict[str, Any]:
        """Salida 2: Recomendaciones personalizadas dentro de un género específico."""
        if not 1 <= top_n <= 50:
            raise ValueError("top_n debe estar entre 1 y 50")
        canon = self._genre_lookup.get(normalize_text(genre))
        if canon is None:
            raise ValueError(f"Género desconocido: '{genre}'")

        ctx = self._score_all_items_for_user(user_id)
        mask = ctx["unseen_mask"] & self._genre_masks[canon]
        cand_idx = np.flatnonzero(mask)
        if len(cand_idx) == 0:
            # Si ya tiene todos los libros del género, permite mostrar el catálogo del género
            cand_idx = np.flatnonzero(self._genre_masks[canon])

        scores = ctx["final_score"][cand_idx]
        order = np.lexsort((self.book_ids[cand_idx], -self.pop_score[cand_idx], -scores))
        chosen = cand_idx[order][:top_n]

        return {
            "user_id": int(user_id),
            "genre": canon,
            "mode": ctx["mode"],
            "alpha": ctx["alpha"],
            "total_in_genre": int(self._genre_masks[canon].sum()),
            "recommendations": [
                self._format_book_recommendation(int(i), ctx, forced_genre=canon) for i in chosen
            ],
        }

    # Salida 3: Detalle del Libro ("Lectores similares también...")
    def compute_item_similarities(
        self,
        book_id: int,
        min_overlap: int = DEFAULT_ITEM_MIN_OVERLAP,
    ) -> list[dict[str, Any]]:
        """Calcula similitudes Ítem-Ítem exactas sobre la intersección U_ij ."""
        min_ov = max(int(min_overlap), MIN_ALLOWED_OVERLAP)
        with self._state_lock:
            target_idx = self._item_idx_of_id.get(int(book_id))
            if target_idx is None:
                raise ValueError(f"Libro {book_id} no encontrado.")
            if self._R.shape[0] == 0:
                return []

            target_col = self._R[:, target_idx].astype(np.float64)
            t_mask = ~np.isnan(target_col)
            n_target_users = int(t_mask.sum())
            if n_target_users < min_ov:
                return []

            # Submatriz únicamente para los usuarios que calificaron el libro objetivo
            sub_R = self._R[t_mask, :].astype(np.float64)  # (n_target_users, n_items)
            valid = ~np.isnan(sub_R)
            overlap = valid.sum(axis=0)

            cand_mask = overlap >= min_ov
            cand_mask[target_idx] = False
            cand_cols = np.flatnonzero(cand_mask)
            if len(cand_cols) == 0:
                return []

            t_ratings = target_col[t_mask][:, None]
            diffs = np.abs(sub_R[:, cand_cols] - t_ratings)
            ov_cand = overlap[cand_cols].astype(np.float64)

            with np.errstate(invalid="ignore"):
                d_cheb = np.nanmax(diffs, axis=0)
                d_msd = np.nanmean(diffs ** 2, axis=0)

            sim_cheb = 1.0 / (1.0 + d_cheb)
            sim_msd_hyp = 1.0 / (1.0 + d_msd)
            sim_msd_norm = np.clip(1.0 - (d_msd / SCALE_RANGE_SQ), 0.0, 1.0)
            union_users = np.maximum(n_target_users + self._item_counts[cand_cols] - ov_cand, 1.0)
            jaccard = ov_cand / union_users
            jmsd = jaccard * sim_msd_norm

            # Ordenar por JMSD y desempate por sim_msd_norm
            order = np.argsort(-(jmsd * 1000.0 + sim_msd_norm))
            out: list[dict[str, Any]] = []
            for pos in order:
                c_idx = int(cand_cols[pos])
                out.append({
                    "item_idx": c_idx,
                    "book_id": int(self.book_ids[c_idx]),
                    "overlap_users": int(ov_cand[pos]),
                    "chebyshev_dist": round(float(d_cheb[pos]), 4),
                    "sim_chebyshev": round(float(sim_cheb[pos]), 4),
                    "msd_value": round(float(d_msd[pos]), 4),
                    "sim_msd_hyp": round(float(sim_msd_hyp[pos]), 4),
                    "sim_msd_norm": round(float(sim_msd_norm[pos]), 4),
                    "jaccard": round(float(jaccard[pos]), 4),
                    "jmsd": round(float(jmsd[pos]), 4),
                })
            return out

    def recommend_similar_books(
        self,
        book_id: int,
        top_n: int = 8,
        min_overlap: int = DEFAULT_ITEM_MIN_OVERLAP,
    ) -> dict[str, Any]:
        """Salida 3: 'Lectores similares también agregaron...'."""
        if not 1 <= top_n <= 30:
            raise ValueError("top_n debe estar entre 1 y 30")

        with self._state_lock:
            target_idx = self._item_idx_of_id.get(int(book_id))
            if target_idx is None:
                # Libro del catálogo ampliado (in_recommender = 0): proyección TF-IDF de solo lectura
                conn = get_connection(self._db_path)
                try:
                    ext_row = conn.execute(
                        "SELECT book_id, title, author, genres, primary_genre, synopsis "
                        "FROM books WHERE book_id = ?",
                        (int(book_id),),
                    ).fetchone()
                finally:
                    conn.close()
                if ext_row is None:
                    raise ValueError(f"Libro {book_id} no encontrado.")

                ext_genres = str(ext_row["genres"] or "")
                ext_pg = str(ext_row["primary_genre"] or "")
                ext_author = str(ext_row["author"] or "")
                ext_syn = str(ext_row["synopsis"] or "")
                q_text = f"{(ext_genres + ' ') * 3}{(ext_pg + ' ') * 2}{(ext_author + ' ') * 2}{ext_syn}"
                q_vec = self._cbf_vec.transform([q_text])
                cbf_sims = (self._X_cbf @ q_vec.T).toarray().ravel().astype(np.float64)
                pos_cbf = cbf_sims[cbf_sims > 0]
                cbf_scale = float(np.percentile(pos_cbf, 99.5)) if len(pos_cbf) > 0 else 1.0
                cbf_norm = np.clip(cbf_sims / max(cbf_scale, 1e-9), 0.0, 1.0)

                combined = 0.85 * cbf_norm + 0.15 * self.pop_score
                cand_idx = np.flatnonzero(combined > 0)
                order = np.lexsort((self.book_ids[cand_idx], -self.pop_score[cand_idx], -combined[cand_idx]))
                chosen = cand_idx[order][:top_n]
                results: list[dict[str, Any]] = []
                for idx in chosen:
                    idx_int = int(idx)
                    r = self.books_df.iloc[idx_int]
                    synopsis = str(r["synopsis"])
                    snippet = (
                        synopsis if len(synopsis) <= SNIPPET_CHARS
                        else synopsis[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"
                    )
                    sc = float(combined[idx_int])
                    results.append({
                        "book_id": int(r["book_id"]),
                        "title": str(r["title"]),
                        "author": str(r["author"]),
                        "genres": self._genre_lists[idx_int],
                        "primary_genre": str(r["primary_genre"]),
                        "publish_year": None if pd.isna(r["publish_year"]) else int(r["publish_year"]),
                        "pages": None if pd.isna(r["pages"]) else int(r["pages"]),
                        "avg_rating": None if pd.isna(r["avg_rating"]) else float(r["avg_rating"]),
                        "ratings_count": int(r["ratings_count"]),
                        "synopsis_snippet": snippet,
                        "similarity": {
                            "hybrid_score": round(sc, 4),
                            "match_percent": int(round(np.clip(sc, 0.0, 1.0) * 100)),
                            "mode": "content_only",
                            "sim_content": round(float(cbf_norm[idx_int]), 4),
                            "overlap_users": 0,
                            "msd_value": None,
                            "sim_msd_norm": None,
                            "sim_msd_hyp": None,
                            "jmsd": None,
                            "sim_chebyshev": None,
                            "reason": f"Similitud temática y de género ({r['primary_genre']}) por contenido TF-IDF",
                        },
                    })
                return {
                    "target_book": {
                        "book_id": int(ext_row["book_id"]),
                        "title": str(ext_row["title"]),
                        "author": str(ext_row["author"]),
                        "primary_genre": ext_pg,
                    },
                    "min_overlap_used": max(int(min_overlap), MIN_ALLOWED_OVERLAP),
                    "collaborative_candidates": 0,
                    "similar_books": results,
                }

            # 1) Similitud de contenido TF-IDF del libro objetivo contra todo el catálogo
            cbf_sims = (self._X_cbf @ self._X_cbf[target_idx].T).toarray().ravel().astype(np.float64)
            cbf_sims[target_idx] = -1.0
            pos_cbf = cbf_sims[cbf_sims > 0]
            cbf_scale = float(np.percentile(pos_cbf, 99.5)) if len(pos_cbf) > 0 else 1.0
            cbf_norm = np.clip(cbf_sims / max(cbf_scale, 1e-9), 0.0, 1.0)

            # 2) Similitud colaborativa Ítem-Ítem con fallback a min_overlap=2 en libros raros
            used_overlap = max(int(min_overlap), MIN_ALLOWED_OVERLAP)
            cf_list = self.compute_item_similarities(book_id, min_overlap=used_overlap)
            if len(cf_list) < top_n and used_overlap > MIN_ALLOWED_OVERLAP:
                used_overlap = MIN_ALLOWED_OVERLAP
                cf_list = self.compute_item_similarities(book_id, min_overlap=used_overlap)

            cf_by_idx = {row["item_idx"]: row for row in cf_list}

            # 3) Fusión Híbrida Ítem-Ítem
            cf_score = np.zeros(self.n_items, dtype=np.float64)
            for c_idx, info in cf_by_idx.items():
                # Combina exactitud MSD con factor de confianza por cantidad de co-lectores
                support_conf = info["overlap_users"] / (info["overlap_users"] + 3.0)
                cf_score[c_idx] = (0.75 * info["sim_msd_norm"] + 0.25 * min(1.0, info["jmsd"] * 4.0)) * support_conf

            has_cf = cf_score > 0
            combined = np.where(
                has_cf,
                ITEM_W_CF * cf_score + ITEM_W_CBF * cbf_norm,
                0.85 * cbf_norm + 0.15 * self.pop_score,
            )
            combined[target_idx] = -1.0

            # Si el libro tiene vecinos colaborativos suficientes, priorizamos los híbridos
            if int(has_cf.sum()) >= top_n:
                cand_idx = np.flatnonzero(has_cf)
            else:
                cand_idx = np.flatnonzero(combined > 0)

            order = np.lexsort((self.book_ids[cand_idx], -self.pop_score[cand_idx], -combined[cand_idx]))
            chosen = cand_idx[order][:top_n]

            target_row = self.books_df.iloc[target_idx]
            results: list[dict[str, Any]] = []
            for idx in chosen:
                idx_int = int(idx)
                r = self.books_df.iloc[idx_int]
                synopsis = str(r["synopsis"])
                snippet = (
                    synopsis if len(synopsis) <= SNIPPET_CHARS
                    else synopsis[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"
                )
                cf_info = cf_by_idx.get(idx_int)
                sc = float(combined[idx_int])

                if cf_info:
                    mode = "item_hybrid"
                    reason = (
                        f"{cf_info['overlap_users']} lectores en común "
                        f"género {r['primary_genre']}"
                    )
                else:
                    mode = "content_only"
                    reason = f"Similitud temática y de género ({r['primary_genre']}) por contenido TF-IDF"

                results.append({
                    "book_id": int(r["book_id"]),
                    "title": str(r["title"]),
                    "author": str(r["author"]),
                    "genres": self._genre_lists[idx_int],
                    "primary_genre": str(r["primary_genre"]),
                    "publish_year": None if pd.isna(r["publish_year"]) else int(r["publish_year"]),
                    "pages": None if pd.isna(r["pages"]) else int(r["pages"]),
                    "avg_rating": None if pd.isna(r["avg_rating"]) else float(r["avg_rating"]),
                    "ratings_count": int(r["ratings_count"]),
                    "synopsis_snippet": snippet,
                    "similarity": {
                        "hybrid_score": round(sc, 4),
                        "match_percent": int(round(np.clip(sc, 0.0, 1.0) * 100)),
                        "mode": mode,
                        "sim_content": round(float(cbf_norm[idx_int]), 4),
                        "overlap_users": cf_info["overlap_users"] if cf_info else 0,
                        "msd_value": cf_info["msd_value"] if cf_info else None,
                        "sim_msd_norm": cf_info["sim_msd_norm"] if cf_info else None,
                        "sim_msd_hyp": cf_info["sim_msd_hyp"] if cf_info else None,
                        "jmsd": cf_info["jmsd"] if cf_info else None,
                        "sim_chebyshev": cf_info["sim_chebyshev"] if cf_info else None,
                        "reason": reason,
                    },
                })

            return {
                "target_book": {
                    "book_id": int(target_row["book_id"]),
                    "title": str(target_row["title"]),
                    "author": str(target_row["author"]),
                    "primary_genre": str(target_row["primary_genre"]),
                },
                "min_overlap_used": used_overlap,
                "collaborative_candidates": len(cf_list),
                "similar_books": results,
            }

# --------------------------------------------------------------------------- #
# Instancia compartida
# --------------------------------------------------------------------------- #
_recommender: HybridBookRecommender | None = None
_lock = threading.Lock()

def get_recommender() -> HybridBookRecommender:
    global _recommender
    if _recommender is None:
        with _lock:
            if _recommender is None:
                _recommender = HybridBookRecommender()
    return _recommender
