"""Capa de persistencia SQLite del recomendador de libros.

Contiene:
  * Ruta y conexión a la base de datos local (``booksphere_community.db``).
  * Esquema relacional (``books``, ``users``, ``reading_list``) e índices.
  * Utilidades de hashing/verificación de contraseñas (PBKDF2-SHA256, solo stdlib)."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

BASE_DIR = Path(__file__).resolve().parent.parent          # .../backend
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.environ.get("RECO_DB_PATH", DATA_DIR / "booksphere_community.db"))

READING_STATUSES = ("por_leer", "leyendo", "leido")

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    book_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    author         TEXT    NOT NULL,
    genres         TEXT    NOT NULL,              -- géneros canónicos separados por coma
    primary_genre  TEXT    NOT NULL,              -- género principal (pestaña de Géneros)
    synopsis       TEXT,
    publish_year   INTEGER,
    pages          INTEGER,
    isbn           TEXT,
    avg_rating     REAL,                          -- calificación global promedio (0-5)
    ratings_count  INTEGER NOT NULL DEFAULT 0,    -- total de valoraciones globales
    catalog_source TEXT    NOT NULL DEFAULT 'amazon_2023',
    in_recommender INTEGER NOT NULL DEFAULT 1,    -- 1 = núcleo recomendador, 0 = catálogo ampliado solo búsqueda
    external_key   TEXT,                          -- parent_asin (Amazon) u Open Library work key
    UNIQUE (title, author)
);

CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT,                         -- NULL = cuenta sin login (lector de la comunidad)
    display_name    TEXT,
    favorite_genres TEXT    NOT NULL DEFAULT '',  -- separados por coma
    is_seed_user    INTEGER NOT NULL DEFAULT 0,   -- 1 = proviene del dataset Amazon Reviews 2023
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reading_list (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    book_id     INTEGER NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
    status      TEXT    NOT NULL CHECK (status IN ('por_leer', 'leyendo', 'leido')),
    rating      REAL    CHECK (rating IS NULL OR (rating >= 1.0 AND rating <= 5.0)),
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, book_id)
);

CREATE INDEX IF NOT EXISTS idx_books_primary_genre  ON books (primary_genre);
CREATE INDEX IF NOT EXISTS idx_books_ratings_count  ON books (ratings_count DESC);
CREATE INDEX IF NOT EXISTS idx_books_year           ON books (publish_year);
CREATE INDEX IF NOT EXISTS idx_books_in_recommender ON books (in_recommender);
CREATE INDEX IF NOT EXISTS idx_reading_user         ON reading_list (user_id);
CREATE INDEX IF NOT EXISTS idx_reading_book         ON reading_list (book_id);
"""

def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Abre una conexión con filas tipo diccionario y claves foráneas activas."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

@contextmanager
def db_session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager: commit si todo sale bien, rollback si hay excepción."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

_BOOKS_MIGRATION_COLS: tuple[tuple[str, str], ...] = (
    ("catalog_source", "TEXT NOT NULL DEFAULT 'amazon_2023'"),
    ("in_recommender", "INTEGER NOT NULL DEFAULT 1"),
    ("external_key", "TEXT"),
)


def init_db(db_path: Path | str | None = None) -> None:
    """Crea las tablas e índices si no existen y migra columnas retrocompatibles en BD existente."""
    with db_session(db_path) as conn:
        conn.executescript(SCHEMA)
        existing_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(books)").fetchall()
        }
        for col_name, col_def in _BOOKS_MIGRATION_COLS:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE books ADD COLUMN {col_name} {col_def}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_books_in_recommender ON books (in_recommender)")

# --------------------------------------------------------------------------- #
# Contraseñas (PBKDF2-SHA256 con sal aleatoria por usuario)
# --------------------------------------------------------------------------- #
_PBKDF2_ITERATIONS = 200_000

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"

def verify_password(password: str, stored: str | None) -> bool:
    """Devuelve False si la cuenta no tiene contraseña (lector sin login)."""
    if not stored:
        return False
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)
