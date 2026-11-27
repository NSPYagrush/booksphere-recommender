"""Servidor API REST con FastAPI — Comunidad de Lectura y Recomendador Híbrido.

Ejecutar servidor local de desarrollo (desde "backend/"):
    python -m uvicorn app.main:app --reload --port 8000

Documentación interactiva Swagger UI (RNF-10):
    http://localhost:8000/docs"""
from __future__ import annotations

import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .community import (
    PUBLIC_LIST_MAX_BOOKS,
    get_community_favorites,
    get_user_neighborhood,
    get_user_public_top_books,
)
from .database import (
    DB_PATH,
    READING_STATUSES,
    db_session,
    get_connection,
    hash_password,
    init_db,
    verify_password,
)
from .evaluation import run_offline_evaluation
from .recommender import (
    IMPLICIT_RATING_BY_STATUS,
    HybridBookRecommender,
    effective_rating,
)
from .search_engine import BookSearchEngine

# --------------------------------------------------------------------------- #
# Esquemas Pydantic (entrada tipada para Swagger / validación automática)
# --------------------------------------------------------------------------- #
class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40, description="Nombre de usuario único")
    password: str = Field(..., min_length=4, max_length=128, description="Contraseña de la cuenta")
    display_name: str | None = Field(default=None, max_length=60, description="Nombre visible en la comunidad")
    favorite_genres: list[str] = Field(..., min_length=1, max_length=8, description="Géneros favoritos iniciales")

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=40)
    password: str = Field(..., min_length=1, max_length=128)

class FavoriteGenresUpdate(BaseModel):
    favorite_genres: list[str] = Field(..., min_length=1, max_length=8)

class ReadingListUpsertRequest(BaseModel):
    status: Literal["por_leer", "leyendo", "leido"] = Field(default="por_leer")
    rating: int | None = Field(default=None, ge=1, le=5, description="Calificación explícita 1-5 estrellas")

# --------------------------------------------------------------------------- #
# Fábrica de la aplicación FastAPI
# --------------------------------------------------------------------------- #
def create_app(db_path: Path | str | None = None) -> FastAPI:
    resolved_db = Path(db_path) if db_path else DB_PATH

    state: dict[str, Any] = {
        "engine": None,
        "recommender": None,
        "eval_cache": None,
        "sessions": {},          # token -> user_id
        "lock": threading.RLock(),
    }

    def _get_engine() -> BookSearchEngine:
        with state["lock"]:
            if state["engine"] is None:
                state["engine"] = BookSearchEngine(resolved_db)
            return state["engine"]

    def _get_recommender() -> HybridBookRecommender:
        with state["lock"]:
            if state["recommender"] is None:
                state["recommender"] = HybridBookRecommender(resolved_db)
            return state["recommender"]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(resolved_db)
        # Pre-cargar buscador y recomendador al arrancar el servidor
        try:
            _get_engine()
            _get_recommender()
        except RuntimeError:
            pass
        yield

    app = FastAPI(
        title="BookSphere — Buscador IR y Recomendador Híbrido de Libros",
        description=(
            "API REST del Trabajo Final de la materia Web Social"),
        version="1.3.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    #  Helpers de sesión/BD
    def _extract_token(authorization: str | None) -> str | None:
        if not authorization:
            return None
        parts = authorization.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return authorization.strip()

    def _optional_user_id(authorization: str | None = Header(default=None)) -> int | None:
        token = _extract_token(authorization)
        if not token:
            return None
        with state["lock"]:
            return state["sessions"].get(token)

    def _require_user_id(authorization: str | None = Header(default=None)) -> int:
        uid = _optional_user_id(authorization)
        if uid is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sesión no válida o expirada. Inicie sesión nuevamente.",
            )
        return uid

    def _user_profile_dict(user_id: int) -> dict[str, Any]:
        conn = get_connection(resolved_db)
        try:
            row = conn.execute(
                "SELECT user_id, username, display_name, favorite_genres, is_seed_user, created_at "
                "FROM users WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Usuario no encontrado.")

            counts_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM reading_list WHERE user_id = ? GROUP BY status",
                (int(user_id),),
            ).fetchall()
            by_status = {s: 0 for s in READING_STATUSES}
            for r in counts_rows:
                by_status[str(r["status"])] = int(r["n"])
            total_books = sum(by_status.values())
            rated_books = int(
                conn.execute(
                    "SELECT COUNT(*) FROM reading_list WHERE user_id = ? AND rating IS NOT NULL",
                    (int(user_id),),
                ).fetchone()[0]
            )
            favs = [g.strip() for g in str(row["favorite_genres"] or "").split(",") if g.strip()]
            return {
                "user_id": int(row["user_id"]),
                "username": str(row["username"]),
                "display_name": str(row["display_name"] or row["username"]),
                "favorite_genres": favs,
                "is_seed_user": bool(row["is_seed_user"]),
                "created_at": str(row["created_at"]),
                "stats": {
                    "total_books": total_books,
                    "rated_books": rated_books,
                    "by_status": by_status,
                    "recommender_mode": "cold_start" if total_books == 0 else "hybrid",
                },
            }
        finally:
            conn.close()

    def _user_reading_map(user_id: int | None) -> dict[int, dict[str, Any]]:
        if user_id is None:
            return {}
        conn = get_connection(resolved_db)
        try:
            rows = conn.execute(
                "SELECT book_id, status, rating FROM reading_list WHERE user_id = ?",
                (int(user_id),),
            ).fetchall()
            return {
                int(r["book_id"]): {
                    "status": str(r["status"]),
                    "rating": None if r["rating"] is None else int(r["rating"]),
                    "effective_rating": effective_rating(str(r["status"]), r["rating"]),
                    "is_implicit": r["rating"] is None,
                }
                for r in rows
            }
        finally:
            conn.close()

    # 0. Salud / Info
    @app.get("/api/health", tags=["Sistema"])
    def health() -> dict[str, Any]:
        engine = _get_engine()
        rec = _get_recommender()
        return {
            "status": "ok",
            "books_indexed": engine.n,
            "recommender_core_books": rec.n_items,
            "community_users": len(rec.user_ids),
            "implicit_rating_rules": IMPLICIT_RATING_BY_STATUS,
            "public_list_max_books": PUBLIC_LIST_MAX_BOOKS,
        }

    # 1. Autenticación
    @app.post("/api/auth/register", status_code=status.HTTP_201_CREATED, tags=["Autenticación"])
    def register(payload: RegisterRequest) -> dict[str, Any]:
        username = payload.username.strip()
        if not username or " " in username:
            raise HTTPException(status_code=400, detail="El nombre de usuario no puede contener espacios.")

        engine = _get_engine()
        valid_genres = {g["genre"] for g in engine.list_genres()}
        chosen_genres = [g.strip() for g in payload.favorite_genres if g.strip() in valid_genres]
        if not chosen_genres:
            raise HTTPException(
                status_code=400,
                detail="Debe seleccionar al menos un género válido del catálogo.",
            )

        display_name = (payload.display_name or "").strip() or username
        pw_hash = hash_password(payload.password)

        try:
            with db_session(resolved_db) as conn:
                cur = conn.execute(
                    "INSERT INTO users (username, password_hash, display_name, favorite_genres, is_seed_user) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (username, pw_hash, display_name, ", ".join(chosen_genres)),
                )
                new_uid = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Ese nombre de usuario ya está registrado.")

        token = secrets.token_urlsafe(32)
        with state["lock"]:
            state["sessions"][token] = new_uid

        return {"token": token, "user": _user_profile_dict(new_uid)}

    @app.post("/api/auth/login", tags=["Autenticación"])
    def login(payload: LoginRequest) -> dict[str, Any]:
        username = payload.username.strip()
        conn = get_connection(resolved_db)
        try:
            row = conn.execute(
                "SELECT user_id, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        finally:
            conn.close()

        if row is None or not verify_password(payload.password, row["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Usuario o contraseña incorrectos.",
            )

        uid = int(row["user_id"])
        token = secrets.token_urlsafe(32)
        with state["lock"]:
            state["sessions"][token] = uid

        return {"token": token, "user": _user_profile_dict(uid)}

    @app.post("/api/auth/logout", tags=["Autenticación"])
    def logout(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        token = _extract_token(authorization)
        if token:
            with state["lock"]:
                state["sessions"].pop(token, None)
        return {"logged_out": True}

    @app.get("/api/auth/me", tags=["Autenticación"])
    def get_me(user_id: int = Depends(_require_user_id)) -> dict[str, Any]:
        return {"user": _user_profile_dict(user_id)}

    @app.put("/api/auth/favorite-genres", tags=["Autenticación"])
    def update_favorite_genres(
        payload: FavoriteGenresUpdate,
        user_id: int = Depends(_require_user_id),
    ) -> dict[str, Any]:
        engine = _get_engine()
        valid_genres = {g["genre"] for g in engine.list_genres()}
        chosen = [g.strip() for g in payload.favorite_genres if g.strip() in valid_genres]
        if not chosen:
            raise HTTPException(status_code=400, detail="Seleccione al menos un género válido.")
        with db_session(resolved_db) as conn:
            conn.execute(
                "UPDATE users SET favorite_genres = ? WHERE user_id = ?",
                (", ".join(chosen), int(user_id)),
            )
        return {"user": _user_profile_dict(user_id)}

    # 2. Buscador y Catálogo
    @app.get("/api/books/genres", tags=["Buscador y Catálogo"])
    def list_genres() -> dict[str, Any]:
        engine = _get_engine()
        rec = _get_recommender()
        rec_genres = set(rec._genre_masks.keys())
        return {
            "genres": [g for g in engine.list_genres() if g["genre"] in rec_genres]
        }

    @app.get("/api/books/search", tags=["Buscador y Catálogo"])
    def search_books(
        q: str = Query(default="", description="Búsqueda textual por título y/o autor"),
        genre: str | None = Query(default=None, description="Filtro por género"),
        min_rating: float | None = Query(default=None, ge=0.0, le=5.0),
        max_rating: float | None = Query(default=None, ge=0.0, le=5.0),
        sort_by: Literal["relevance", "popularity", "rating"] = Query(default="relevance"),
        exact: bool = Query(default=False, description="Coincidencia en título o autor"),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        user_id: int | None = Depends(_optional_user_id),
    ) -> dict[str, Any]:
        engine = _get_engine()
        try:
            res = engine.search(
                query=q,
                genre=genre,
                min_rating=min_rating,
                max_rating=max_rating,
                sort_by=sort_by,
                exact=exact,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        user_map = _user_reading_map(user_id)
        for item in res["results"]:
            item["user_entry"] = user_map.get(item["book_id"])
        return res

    @app.get("/api/books/{book_id}", tags=["Buscador y Catálogo"])
    def get_book_detail(
        book_id: int,
        user_id: int | None = Depends(_optional_user_id),
    ) -> dict[str, Any]:
        engine = _get_engine()
        book = engine.get_book(book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="Libro no encontrado.")
        user_map = _user_reading_map(user_id)
        book["user_entry"] = user_map.get(book_id)
        return book

    # 3. Lista de Lectura Dinámica
    @app.get("/api/reading-list", tags=["Lista de Lectura"])
    def get_reading_list(
        status_filter: Literal["por_leer", "leyendo", "leido"] | None = Query(default=None, alias="status"),
        user_id: int = Depends(_require_user_id),
    ) -> dict[str, Any]:
        conn = get_connection(resolved_db)
        try:
            sql = """
                SELECT rl.book_id, rl.status, rl.rating, rl.updated_at,
                       b.title, b.author, b.genres, b.primary_genre, b.publish_year,
                       b.pages, b.avg_rating, b.ratings_count, b.synopsis,
                       COALESCE(b.catalog_source, 'amazon_2023') AS catalog_source,
                       COALESCE(b.in_recommender, 1) AS in_recommender
                FROM reading_list rl
                JOIN books b ON b.book_id = rl.book_id
                WHERE rl.user_id = ?
            """
            params: list[Any] = [int(user_id)]
            if status_filter:
                sql += " AND rl.status = ?"
                params.append(status_filter)
            sql += " ORDER BY rl.updated_at DESC, COALESCE(rl.rating, 0) DESC, b.title ASC"
            rows = conn.execute(sql, params).fetchall()

            counts_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM reading_list WHERE user_id = ? GROUP BY status",
                (int(user_id),),
            ).fetchall()
        finally:
            conn.close()

        counts = {s: 0 for s in READING_STATUSES}
        for r in counts_rows:
            counts[str(r["status"])] = int(r["n"])

        items: list[dict[str, Any]] = []
        for r in rows:
            syn = str(r["synopsis"] or "")
            snippet = syn if len(syn) <= 200 else syn[:200].rsplit(" ", 1)[0] + "…"
            eff = effective_rating(str(r["status"]), r["rating"])
            items.append({
                "book_id": int(r["book_id"]),
                "title": str(r["title"]),
                "author": str(r["author"]),
                "genres": [g.strip() for g in str(r["genres"]).split(",") if g.strip()],
                "primary_genre": str(r["primary_genre"]),
                "publish_year": None if r["publish_year"] is None else int(r["publish_year"]),
                "pages": None if r["pages"] is None else int(r["pages"]),
                "avg_rating": None if r["avg_rating"] is None else float(r["avg_rating"]),
                "ratings_count": int(r["ratings_count"]),
                "catalog_source": str(r["catalog_source"]),
                "in_recommender": bool(int(r["in_recommender"])),
                "synopsis_snippet": snippet,
                "status": str(r["status"]),
                "rating": None if r["rating"] is None else int(r["rating"]),
                "effective_rating": eff,
                "is_implicit_rating": r["rating"] is None,
                "updated_at": str(r["updated_at"]),
            })

        return {
            "user_id": int(user_id),
            "total": sum(counts.values()),
            "counts_by_status": counts,
            "implicit_rating_rules": IMPLICIT_RATING_BY_STATUS,
            "items": items,
        }

    @app.put("/api/reading-list/{book_id}", tags=["Lista de Lectura"])
    def upsert_reading_list_item(
        book_id: int,
        payload: ReadingListUpsertRequest,
        user_id: int = Depends(_require_user_id),
    ) -> dict[str, Any]:
        engine = _get_engine()
        if engine.get_book(book_id) is None:
            raise HTTPException(status_code=404, detail="Libro no encontrado en el catálogo.")

        rating_val = None if payload.rating is None else float(payload.rating)
        with db_session(resolved_db) as conn:
            conn.execute(
                """
                INSERT INTO reading_list (user_id, book_id, status, rating, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, book_id) DO UPDATE SET
                    status = excluded.status,
                    rating = excluded.rating,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(user_id), int(book_id), payload.status, rating_val),
            )

        # Reactividad inmediata en el motor recomendador
        rec = _get_recommender()
        rec.sync_user_row(user_id)

        return {
            "saved": True,
            "book_id": int(book_id),
            "status": payload.status,
            "rating": payload.rating,
            "effective_rating": effective_rating(payload.status, rating_val),
            "is_implicit_rating": payload.rating is None,
        }

    @app.delete("/api/reading-list/{book_id}", tags=["Lista de Lectura"])
    def remove_reading_list_item(
        book_id: int,
        user_id: int = Depends(_require_user_id),
    ) -> dict[str, Any]:
        with db_session(resolved_db) as conn:
            cur = conn.execute(
                "DELETE FROM reading_list WHERE user_id = ? AND book_id = ?",
                (int(user_id), int(book_id)),
            )
            deleted = cur.rowcount > 0

        if deleted:
            _get_recommender().sync_user_row(user_id)
        return {"removed": deleted, "book_id": int(book_id)}

    # 4. Recomendador Híbrido
    @app.get("/api/recommendations/home", tags=["Recomendador Híbrido"])
    def recommendations_home(
        top_n: int = Query(default=15, ge=1, le=100),
        user_id: int = Depends(_require_user_id),
    ) -> dict[str, Any]:
        rec = _get_recommender()
        try:
            res = rec.recommend_for_home(user_id, top_n=top_n)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        user_map = _user_reading_map(user_id)
        for item in res["recommendations"]:
            item["user_entry"] = user_map.get(item["book_id"])
        return res

    @app.get("/api/recommendations/genre/{genre}", tags=["Recomendador Híbrido"])
    def recommendations_by_genre(
        genre: str,
        top_n: int = Query(default=10, ge=1, le=50),
        user_id: int = Depends(_require_user_id),
    ) -> dict[str, Any]:
        rec = _get_recommender()
        try:
            res = rec.recommend_by_genre(user_id, genre=genre, top_n=top_n)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        user_map = _user_reading_map(user_id)
        for item in res["recommendations"]:
            item["user_entry"] = user_map.get(item["book_id"])
        return res

    @app.get("/api/recommendations/similar/{book_id}", tags=["Recomendador"])
    def recommendations_similar_books(
        book_id: int,
        top_n: int = Query(default=8, ge=1, le=30),
        min_overlap: int = Query(default=5, ge=2, le=20),
        user_id: int | None = Depends(_optional_user_id),
    ) -> dict[str, Any]:
        rec = _get_recommender()
        try:
            res = rec.recommend_similar_books(book_id, top_n=top_n, min_overlap=min_overlap)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        user_map = _user_reading_map(user_id)
        for item in res["similar_books"]:
            item["user_entry"] = user_map.get(item["book_id"])
        return res

    # 5. Comunidad
    @app.get("/api/community/neighborhood", tags=["Comunidad"])
    def community_neighborhood(
        limit: int = Query(default=12, ge=1, le=30),
        user_id: int = Depends(_require_user_id),
    ) -> dict[str, Any]:
        rec = _get_recommender()
        try:
            return get_user_neighborhood(user_id, rec, limit=limit, db_path=resolved_db)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/community/users/{target_user_id}/top-books", tags=["Comunidad"])
    def community_user_top_books(
        target_user_id: int,
        user_id: int | None = Depends(_optional_user_id),
    ) -> dict[str, Any]:
        try:
            return get_user_public_top_books(
                target_user_id,
                viewer_user_id=user_id,
                limit=PUBLIC_LIST_MAX_BOOKS,
                db_path=resolved_db,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/community/favorites", tags=["Comunidad"])
    def community_favorites(
        limit: int = Query(default=10, ge=1, le=30),
        user_id: int | None = Depends(_optional_user_id),
    ) -> dict[str, Any]:
        res = get_community_favorites(limit=limit, db_path=resolved_db)
        user_map = _user_reading_map(user_id)
        for key in ("most_read", "highest_rated"):
            for item in res[key]:
                item["user_entry"] = user_map.get(item["book_id"])
        return res

    # 6. Evaluación Offline
    @app.get("/api/evaluation", tags=["Evaluación"])
    def evaluation_metrics(
        recompute: bool = Query(default=False, description="Fuerza el recálculo del split 80/20"),
    ) -> dict[str, Any]:
        with state["lock"]:
            if state["eval_cache"] is None or recompute:
                state["eval_cache"] = run_offline_evaluation(_get_recommender())
            return state["eval_cache"]

    return app

app = create_app()
