"""Módulo de Comunidad: Vecindario de Lectores y Favoritos de la Comunidad.

Funcionalidades:
  1. 'get_user_neighborhood': Calcula los usuarios con gustos más afines mediante similitud 
     Usuario-Usuario MSD/JMSD (reutilizando 'HybridBookRecommender.compute_user_neighbors' con 
     min_overlap >= 2). Para usuarios en arranque en frío (sin suficientes libros co-evaluados 
     aún), sugiere lectores de la comunidad con mayor coincidencia en géneros favoritos.
  2. 'get_user_public_top_books': Devuelve la lista de lectura pública de otro miembro de la 
     comunidad, limitada estrictamente al Top 10 de libros mejor valorados/priorizados por ese usuario.
  3. 'get_community_favorites': Construye la sección "Favoritos de la Comunidad" a partir de las 
     interacciones reales de la tabla 'reading_list':
       * 'most_read': libros presentes en más listas de usuarios.
       * 'highest_rated': libros con mejores puntuaciones ponderadas (media bayesiana comunitaria)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .database import get_connection
from .recommender import (
    DEFAULT_USER_MIN_OVERLAP,
    MIN_ALLOWED_OVERLAP,
    HybridBookRecommender,
    effective_rating,
)

PUBLIC_LIST_MAX_BOOKS = 10
MIN_COMMUNITY_VOTES_FOR_TOP_RATED = 10
SNIPPET_CHARS = 200

def _split_genres(raw: str | None) -> list[str]:
    return [g.strip() for g in str(raw or "").split(",") if g.strip()]


def _snippet(text: str | None) -> str:
    s = str(text or "")
    return s if len(s) <= SNIPPET_CHARS else s[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"

# --------------------------------------------------------------------------- #
# 1. Vecindario de Lectores
# --------------------------------------------------------------------------- #
def get_user_neighborhood(
    user_id: int,
    recommender: HybridBookRecommender,
    limit: int = 12,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Devuelve 'Tu vecindario de lectores' con porcentaje de afinidad y géneros compartidos."""
    if not 1 <= limit <= 30:
        raise ValueError("limit debe estar entre 1 y 30")

    path = db_path if db_path is not None else recommender._db_path
    conn = get_connection(path)
    try:
        me = conn.execute(
            "SELECT user_id, username, display_name, favorite_genres FROM users WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if me is None:
            raise ValueError(f"Usuario {user_id} no encontrado.")
        my_favs = set(_split_genres(me["favorite_genres"]))

        # Intentar primero vecindario colaborativo real (MSD/JMSD) con min_overlap >= 3, luego >= 2
        nb_raw = recommender.compute_user_neighbors(user_id, min_overlap=DEFAULT_USER_MIN_OVERLAP, top_k=limit)
        min_ov_used = DEFAULT_USER_MIN_OVERLAP
        if not nb_raw:
            nb_raw = recommender.compute_user_neighbors(user_id, min_overlap=MIN_ALLOWED_OVERLAP, top_k=limit)
            min_ov_used = MIN_ALLOWED_OVERLAP

        if nb_raw:
            ids = [n["user_id"] for n in nb_raw]
            placeholders = ",".join("?" for _ in ids)
            meta_rows = conn.execute(
                f"""
                SELECT u.user_id, u.username, u.display_name, u.favorite_genres,
                       COUNT(rl.id) AS total_books
                FROM users u
                LEFT JOIN reading_list rl ON rl.user_id = u.user_id
                WHERE u.user_id IN ({placeholders})
                GROUP BY u.user_id
                """,
                ids,
            ).fetchall()
            meta_by_id = {int(r["user_id"]): r for r in meta_rows}

            neighbors: list[dict[str, Any]] = []
            for n in nb_raw:
                u_meta = meta_by_id.get(n["user_id"])
                if not u_meta:
                    continue
                u_favs = _split_genres(u_meta["favorite_genres"])
                shared = [g for g in u_favs if g in my_favs]
                neighbors.append({
                    "user_id": int(n["user_id"]),
                    "username": str(u_meta["username"]),
                    "display_name": str(u_meta["display_name"] or u_meta["username"]),
                    "favorite_genres": u_favs,
                    "shared_genres": shared,
                    "total_books": int(u_meta["total_books"]),
                    "public_list_shown_max": min(int(u_meta["total_books"]), PUBLIC_LIST_MAX_BOOKS),
                    "match_type": "collaborative_msd",
                    "affinity_percent": n["affinity_percent"],
                    "overlap_books": n["overlap_books"],
                    "msd_value": n["msd_value"],
                    "sim_msd_norm": n["sim_msd_norm"],
                    "jmsd": n["jmsd"],
                    "sim_chebyshev": n["sim_chebyshev"],
                })

            return {
                "user_id": int(user_id),
                "mode": "collaborative_msd",
                "min_overlap_used": min_ov_used,
                "neighbors": neighbors,
            }

        # Fallback en Cold Start: lectores de la comunidad con mayor coincidencia en géneros favoritos
        other_rows = conn.execute(
            """
            SELECT u.user_id, u.username, u.display_name, u.favorite_genres,
                   COUNT(rl.id) AS total_books
            FROM users u
            JOIN reading_list rl ON rl.user_id = u.user_id
            WHERE u.user_id != ?
            GROUP BY u.user_id
            HAVING total_books >= 5
            """,
            (int(user_id),),
        ).fetchall()

        scored: list[tuple[float, int, dict[str, Any]]] = []
        for r in other_rows:
            u_favs = _split_genres(r["favorite_genres"])
            u_set = set(u_favs)
            shared = [g for g in u_favs if g in my_favs]
            union = my_favs | u_set
            jaccard = (len(shared) / len(union)) if union else 0.0
            if my_favs and not shared:
                continue
            affinity = round(jaccard * 100.0, 1) if my_favs else 50.0
            scored.append((
                jaccard,
                int(r["total_books"]),
                {
                    "user_id": int(r["user_id"]),
                    "username": str(r["username"]),
                    "display_name": str(r["display_name"] or r["username"]),
                    "favorite_genres": u_favs,
                    "shared_genres": shared,
                    "total_books": int(r["total_books"]),
                    "public_list_shown_max": min(int(r["total_books"]), PUBLIC_LIST_MAX_BOOKS),
                    "match_type": "genre_cold_start",
                    "affinity_percent": affinity,
                    "overlap_books": 0,
                    "msd_value": None,
                    "sim_msd_norm": None,
                    "jmsd": None,
                    "sim_chebyshev": None,
                },
            ))

        scored.sort(key=lambda item: (-item[0], -item[1], item[2]["user_id"]))
        return {
            "user_id": int(user_id),
            "mode": "genre_cold_start",
            "min_overlap_used": 0,
            "neighbors": [item[2] for item in scored[:limit]],
        }
    finally:
        conn.close()

# --------------------------------------------------------------------------- #
# 2. Lista Pública de Otro Lector — Restringida al Top 10
# --------------------------------------------------------------------------- #
def get_user_public_top_books(
    target_user_id: int,
    viewer_user_id: int | None = None,
    limit: int = PUBLIC_LIST_MAX_BOOKS,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Devuelve como máximo el Top 10 de libros de un lector vecino."""
    capped_limit = max(1, min(int(limit), PUBLIC_LIST_MAX_BOOKS))
    conn = get_connection(db_path)
    try:
        u = conn.execute(
            "SELECT user_id, username, display_name, favorite_genres FROM users WHERE user_id = ?",
            (int(target_user_id),),
        ).fetchone()
        if u is None:
            raise ValueError(f"Usuario {target_user_id} no encontrado.")

        total_books = int(
            conn.execute(
                "SELECT COUNT(*) FROM reading_list WHERE user_id = ?",
                (int(target_user_id),),
            ).fetchone()[0]
        )

        viewer_book_ids: set[int] = set()
        if viewer_user_id is not None:
            v_rows = conn.execute(
                "SELECT book_id FROM reading_list WHERE user_id = ?",
                (int(viewer_user_id),),
            ).fetchall()
            viewer_book_ids = {int(r["book_id"]) for r in v_rows}

        rows = conn.execute(
            """
            SELECT rl.book_id, rl.status, rl.rating,
                   COALESCE(
                       rl.rating,
                       CASE rl.status
                           WHEN 'leido' THEN 4.0
                           WHEN 'leyendo' THEN 3.7
                           ELSE 3.0
                       END
                   ) AS eff_rating,
                   CASE rl.status
                       WHEN 'leido' THEN 3
                       WHEN 'leyendo' THEN 2
                       ELSE 1
                   END AS status_rank,
                   b.title, b.author, b.genres, b.primary_genre, b.publish_year,
                   b.pages, b.avg_rating, b.ratings_count, b.synopsis
            FROM reading_list rl
            JOIN books b ON b.book_id = rl.book_id
            WHERE rl.user_id = ?
            ORDER BY eff_rating DESC, status_rank DESC, b.ratings_count DESC, b.book_id ASC
            LIMIT ?
            """,
            (int(target_user_id), capped_limit),
        ).fetchall()

        top_books: list[dict[str, Any]] = []
        for idx, r in enumerate(rows, start=1):
            bid = int(r["book_id"])
            top_books.append({
                "rank": idx,
                "book_id": bid,
                "title": str(r["title"]),
                "author": str(r["author"]),
                "genres": _split_genres(r["genres"]),
                "primary_genre": str(r["primary_genre"]),
                "publish_year": None if r["publish_year"] is None else int(r["publish_year"]),
                "avg_rating": None if r["avg_rating"] is None else float(r["avg_rating"]),
                "ratings_count": int(r["ratings_count"]),
                "synopsis_snippet": _snippet(r["synopsis"]),
                "neighbor_status": str(r["status"]),
                "neighbor_rating": None if r["rating"] is None else float(r["rating"]),
                "neighbor_effective_rating": effective_rating(str(r["status"]), r["rating"]),
                "in_viewer_list": bid in viewer_book_ids,
            })

        return {
            "user": {
                "user_id": int(u["user_id"]),
                "username": str(u["username"]),
                "display_name": str(u["display_name"] or u["username"]),
                "favorite_genres": _split_genres(u["favorite_genres"]),
            },
            "total_books_in_list": total_books,
            "shown_count": len(top_books),
            "max_public_limit": PUBLIC_LIST_MAX_BOOKS,
            "top_books": top_books,
        }
    finally:
        conn.close()

# --------------------------------------------------------------------------- #
# 3. Favoritos de la Comunidad
# --------------------------------------------------------------------------- #
def get_community_favorites(
    limit: int = 10,
    min_votes_for_rated: int = MIN_COMMUNITY_VOTES_FOR_TOP_RATED,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Devuelve los libros más leídos y los mejor puntuados dentro de la comunidad local."""
    if not 1 <= limit <= 30:
        raise ValueError("limit debe estar entre 1 y 30")

    conn = get_connection(db_path)
    try:
        total_books = int(conn.execute("SELECT COUNT(*) FROM books").fetchone()[0])
        total_users = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        total_interactions = int(conn.execute("SELECT COUNT(*) FROM reading_list").fetchone()[0])
        explicit_count = int(
            conn.execute("SELECT COUNT(*) FROM reading_list WHERE rating IS NOT NULL").fetchone()[0]
        )
        global_mean_row = conn.execute(
            """
            SELECT AVG(COALESCE(
                rating,
                CASE status WHEN 'leido' THEN 4.0 WHEN 'leyendo' THEN 3.7 ELSE 3.0 END
            )) FROM reading_list
            """
        ).fetchone()
        global_c = float(global_mean_row[0]) if global_mean_row and global_mean_row[0] is not None else 3.8

        # 1) Más leídos / presentes en más listas de la comunidad
        most_read_rows = conn.execute(
            """
            SELECT b.book_id, b.title, b.author, b.genres, b.primary_genre,
                   b.publish_year, b.avg_rating, b.ratings_count, b.synopsis,
                   COUNT(rl.id) AS community_readers,
                   ROUND(AVG(COALESCE(
                       rl.rating,
                       CASE rl.status WHEN 'leido' THEN 4.0 WHEN 'leyendo' THEN 3.7 ELSE 3.0 END
                   )), 2) AS community_avg_rating,
                   SUM(CASE WHEN rl.rating >= 4.0 THEN 1 ELSE 0 END) AS high_rating_count
            FROM reading_list rl
            JOIN books b ON b.book_id = rl.book_id
            GROUP BY b.book_id
            ORDER BY community_readers DESC, community_avg_rating DESC, b.ratings_count DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

        # 2) Mejor puntuados por la comunidad
        min_v = max(1, int(min_votes_for_rated))
        highest_rated_rows = conn.execute(
            """
            SELECT b.book_id, b.title, b.author, b.genres, b.primary_genre,
                   b.publish_year, b.avg_rating, b.ratings_count, b.synopsis,
                   COUNT(rl.id) AS community_readers,
                   ROUND(AVG(COALESCE(
                       rl.rating,
                       CASE rl.status WHEN 'leido' THEN 4.0 WHEN 'leyendo' THEN 3.7 ELSE 3.0 END
                   )), 2) AS community_avg_rating,
                   SUM(CASE WHEN rl.rating >= 4.0 THEN 1 ELSE 0 END) AS high_rating_count,
                   (
                       (CAST(COUNT(rl.id) AS REAL) / (COUNT(rl.id) + ?))
                       * AVG(COALESCE(
                           rl.rating,
                           CASE rl.status WHEN 'leido' THEN 4.0 WHEN 'leyendo' THEN 3.7 ELSE 3.0 END
                       ))
                       + (CAST(? AS REAL) / (COUNT(rl.id) + ?)) * ?
                   ) AS community_weighted_score
            FROM reading_list rl
            JOIN books b ON b.book_id = rl.book_id
            GROUP BY b.book_id
            HAVING community_readers >= ?
            ORDER BY community_weighted_score DESC, community_readers DESC
            LIMIT ?
            """,
            (min_v, min_v, min_v, global_c, min_v, int(limit)),
        ).fetchall()

        # Si el dataset sintético es muy pequeño y nadie supera min_v, relaja a >= 1 voto
        if not highest_rated_rows and min_v > 1:
            return get_community_favorites(limit=limit, min_votes_for_rated=1, db_path=db_path)

        def _format_fav(rows: list[Any], include_weighted: bool = False) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for rank, r in enumerate(rows, start=1):
                entry = {
                    "rank": rank,
                    "book_id": int(r["book_id"]),
                    "title": str(r["title"]),
                    "author": str(r["author"]),
                    "genres": _split_genres(r["genres"]),
                    "primary_genre": str(r["primary_genre"]),
                    "publish_year": None if r["publish_year"] is None else int(r["publish_year"]),
                    "avg_rating": None if r["avg_rating"] is None else float(r["avg_rating"]),
                    "ratings_count": int(r["ratings_count"]),
                    "synopsis_snippet": _snippet(r["synopsis"]),
                    "community_readers": int(r["community_readers"]),
                    "community_avg_rating": float(r["community_avg_rating"]),
                    "high_rating_count": int(r["high_rating_count"] or 0),
                }
                if include_weighted:
                    entry["community_weighted_score"] = round(float(r["community_weighted_score"]), 3)
                items.append(entry)
            return items

        return {
            "community_stats": {
                "total_books": total_books,
                "total_users": total_users,
                "total_interactions": total_interactions,
                "explicit_ratings": explicit_count,
                "implicit_entries": total_interactions - explicit_count,
                "community_global_mean": round(global_c, 3),
            },
            "most_read": _format_fav(most_read_rows),
            "highest_rated": _format_fav(highest_rated_rows, include_weighted=True),
            "min_votes_threshold": min_v,
        }
    finally:
        conn.close()
