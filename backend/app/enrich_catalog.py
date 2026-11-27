"""Enriquecimiento offline en dos niveles (Open Library -> SQLite).

Uso independiente (desde ``backend/``):
    python -m app.enrich_catalog [--core-target 12000] [--extended-target 3000] [--reset-extended]

Distribución estricta garantizada:
  * Promociones al núcleo ``in_recommender = 1`` (``catalog_source = 'openlibrary_core'``):
      1. Obras en español original de autores hispanohablantes (``language:spa``, sin
         duplicar las ya localizadas desde Amazon y sin traducciones al inglés).
      2. Clásicos universales y autores contemporáneos clave ausentes en el corte de Amazon
         y obras de Open Library con ``>= 25`` votos (incluidos lanzamientos 2024–2025 votados).
  * Catálogo ampliado ``in_recommender = 0`` (``catalog_source = 'openlibrary_curated'``, +3,000):
      - Exclusivamente lanzamientos recientes ``año > 2023`` (2024–2025) con ``< 25`` votos
        (``avg_rating = NULL``, ``ratings_count = 0``), totalmente deduplicados contra el núcleo.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from .database import db_session, init_db
from .openlibrary_client import (
    HISPANIC_AUTHORS_CANON,
    CatalogDedupeIndex,
    clean_author,
    clean_title,
    deduplicate_books_map,
    is_better_author,
    is_cleaner_title,
    is_non_book_format,
    has_clean_punctuation,
    query_openlibrary,
)

DEFAULT_CORE_TARGET = 12_000
DEFAULT_EXTENDED_TARGET = 3_000
REQUEST_DELAY_SEC = 0.24

# Autores no hispanohablantes icónicos (clásicos y contemporáneos de alta demanda)
NON_HISPANIC_AUTHORS: tuple[tuple[str, str | None], ...] = (
    ("Brandon Sanderson", "Fantasy"),
    ("Rebecca Yarros", "Fantasy"),
    ("Sarah J. Maas", "Fantasy"),
    ("R. F. Kuang", "Fantasy"),
    ("Pierce Brown", "Science Fiction"),
    ("Andy Weir", "Science Fiction"),
    ("Cixin Liu", "Science Fiction"),
    ("Blake Crouch", "Science Fiction"),
    ("Frank Herbert", "Science Fiction"),
    ("Isaac Asimov", "Science Fiction"),
    ("Ursula K. Le Guin", "Science Fiction"),
    ("Octavia E. Butler", "Science Fiction"),
    ("Ray Bradbury", "Science Fiction"),
    ("George Orwell", "Classics"),
    ("Aldous Huxley", "Classics"),
    ("J. R. R. Tolkien", "Fantasy"),
    ("C. S. Lewis", "Fantasy"),
    ("Jane Austen", "Classics"),
    ("Mary Shelley", "Classics"),
    ("Bram Stoker", "Horror"),
    ("Fyodor Dostoevsky", "Classics"),
    ("Leo Tolstoy", "Classics"),
    ("Agatha Christie", "Mystery"),
    ("Arthur Conan Doyle", "Mystery"),
    ("Stephen King", "Horror"),
    ("J. K. Rowling", "Fantasy"),
    ("Neil Gaiman", "Fantasy"),
    ("Terry Pratchett", "Fantasy"),
    ("Patrick Rothfuss", "Fantasy"),
    ("George R. R. Martin", "Fantasy"),
    ("Joe Abercrombie", "Fantasy"),
    ("Robin Hobb", "Fantasy"),
    ("Suzanne Collins", "Young Adult"),
    ("Rick Riordan", "Young Adult"),
    ("Emily Henry", "Romance"),
    ("Colleen Hoover", "Romance"),
    ("Taylor Jenkins Reid", "Romance"),
    ("Sally Rooney", "Fiction"),
    ("Haruki Murakami", "Fiction"),
    ("Kazuo Ishiguro", "Fiction"),
    ("Margaret Atwood", "Fiction"),
    ("Cormac McCarthy", "Fiction"),
    ("Donna Tartt", "Fiction"),
    ("Khaled Hosseini", "Fiction"),
    ("Fredrik Backman", "Fiction"),
    ("Freida McFadden", "Thriller"),
    ("Alex Michaelides", "Thriller"),
    ("Gillian Flynn", "Thriller"),
    ("James Clear", "Self Help"),
    ("Morgan Housel", "Business"),
    ("Cal Newport", "Self Help"),
    ("Yuval Noah Harari", "History"),
    ("Malcolm Gladwell", "Psychology"),
    ("Robert Greene", "Psychology"),
    ("Viktor E. Frankl", "Psychology"),
    ("Marcus Aurelius", "Philosophy"),
    ("Madeline Miller", "Historical Fiction"),
)

# Autores hispanohablantes prioritarios para consultar en idioma original (language:spa)
HISPANIC_QUERY_AUTHORS: tuple[tuple[str, str], ...] = (
    ("Gabriel Garcia Marquez", "Classics"),
    ("Jorge Luis Borges", "Classics"),
    ("Julio Cortazar", "Classics"),
    ("Isabel Allende", "Fiction"),
    ("Miguel de Cervantes", "Classics"),
    ("Carlos Ruiz Zafon", "Mystery"),
    ("Mario Vargas Llosa", "Fiction"),
    ("Roberto Bolano", "Fiction"),
    ("Juan Rulfo", "Classics"),
    ("Octavio Paz", "Poetry"),
    ("Pablo Neruda", "Poetry"),
    ("Laura Esquivel", "Fiction"),
    ("Arturo Perez-Reverte", "Historical Fiction"),
    ("Federico Garcia Lorca", "Poetry"),
    ("Eduardo Galeano", "History"),
    ("Mariana Enriquez", "Horror"),
    ("Samanta Schweblin", "Fiction"),
    ("Javier Marias", "Fiction"),
    ("Maria Duenas", "Historical Fiction"),
    ("Dolores Redondo", "Thriller"),
    ("Javier Castillo", "Thriller"),
    ("Juan Gabriel Vasquez", "Fiction"),
    ("Hector Abad Faciolince", "Biography & Memoir"),
    ("Carlos Fuentes", "Fiction"),
    ("Ernesto Sabato", "Classics"),
    ("Horacio Quiroga", "Classics"),
    ("Adolfo Bioy Casares", "Science Fiction"),
    ("Rosa Montero", "Fiction"),
    ("Almudena Grandes", "Historical Fiction"),
    ("Ruben Dario", "Poetry"),
)

RECENT_GENRE_QUERIES: tuple[tuple[str, str], ...] = (
    ("fantasy", "Fantasy"),
    ("science fiction", "Science Fiction"),
    ("thriller", "Thriller"),
    ("mystery", "Mystery"),
    ("romance", "Romance"),
    ("historical fiction", "Historical Fiction"),
    ("horror", "Horror"),
    ("biography", "Biography & Memoir"),
    ("history", "History"),
    ("self-help", "Self Help"),
    ("science", "Science"),
    ("young adult", "Young Adult"),
    ("business", "Business"),
    ("poetry", "Poetry"),
    ("philosophy", "Philosophy"),
    ("psychology", "Psychology"),
    ("fiction", "Fiction"),
    ("adventure", "Adventure"),
)


def _register_candidate(
    item: dict[str, Any],
    dedupe_idx: CatalogDedupeIndex,
    target_bucket: dict[str, dict[str, Any]],
) -> bool:
    """Registra una obra única usando deduplicación de 2 capas y preservando el mejor título/autor."""
    ck = item["canon_key"]
    ta = (item["title"].lower(), item["author"].lower())
    ek = item["external_key"]

    if ck in target_bucket:
        cur = target_bucket[ck]
        if is_cleaner_title(item["title"], cur["title"]) and ta not in dedupe_idx.seen_ta:
            item["ratings_count"] = max(item["ratings_count"], cur["ratings_count"])
            if not is_better_author(item["author"], cur["author"]):
                item["author"] = cur["author"]
            target_bucket[ck] = item
        return False

    if dedupe_idx.is_duplicate(item["title"], item["author"], ek):
        return False

    dedupe_idx.add(item["title"], item["author"], ek)
    target_bucket[ck] = item
    return True


def enrich_database(
    core_target_total: int = DEFAULT_CORE_TARGET,
    extended_target: int = DEFAULT_EXTENDED_TARGET,
    db_path: Path | str | None = None,
    reset_extended: bool = False,
) -> dict[str, Any]:
    """Completa el núcleo in_recommender=1 (español + clásicos + >=25 votos) y crea el bloque 2024-2025."""
    t0 = time.perf_counter()
    init_db(db_path)

    with db_session(db_path) as conn:
        if reset_extended:
            conn.execute("DELETE FROM books WHERE in_recommender = 0")

        existing = conn.execute(
            "SELECT title, author, external_key, in_recommender FROM books"
        ).fetchall()
        dedupe_idx = CatalogDedupeIndex()
        current_core_count = 0
        current_ext_count = 0
        for r in existing:
            t = str(r["title"] or "")
            a = str(r["author"] or "")
            ek = str(r["external_key"]) if r["external_key"] else None
            dedupe_idx.add(t, a, ek)
            if int(r["in_recommender"]) == 1:
                current_core_count += 1
            else:
                current_ext_count += 1

    core_slots_needed = max(0, core_target_total - current_core_count)
    ext_slots_needed = max(0, extended_target - current_ext_count)
    print(
        f"[4/4] Enriquecimiento Open Library: núcleo actual={current_core_count:,} "
        f"(cupos={core_slots_needed:,}) | ampliado 2024–2025 objetivo=+{ext_slots_needed:,}",
        flush=True,
    )

    spanish_bucket: dict[str, dict[str, Any]] = {}
    recent_voted_bucket: dict[str, dict[str, Any]] = {}
    ext_2024_2025_bucket: dict[str, dict[str, Any]] = {}
    classics_pool: list[dict[str, Any]] = []

    with httpx.Client(follow_redirects=True, timeout=25.0) as client:
        # 1. Obras originalmente en español (language:spa) -> entran al núcleo in_recommender=1
        spa_queries: list[tuple[str, dict[str, Any], str | None]] = [
            (
                f"ES: {author_name}",
                {"author": author_name, "language": "spa", "sort": "readinglog", "limit": 18, "page": 1},
                hint,
            )
            for author_name, hint in HISPANIC_QUERY_AUTHORS
        ] + [
            (
                "ES: Clásicos literatura hispana",
                {"q": 'subject:"Spanish fiction" OR subject:"Latin American fiction" language:spa', "sort": "readinglog", "limit": 60, "page": 1},
                "Fiction",
            ),
        ]
        for label, params, hint in spa_queries:
            if len(spanish_bucket) >= max(core_slots_needed // 2, 350):
                break
            docs = query_openlibrary(
                params,
                client,
                hint_genre=hint,
                expected_lang="es",
                catalog_source="openlibrary_core",
                in_recommender=1,
            )
            for item in docs:
                if len(spanish_bucket) >= max(core_slots_needed // 2, 350):
                    break
                _register_candidate(item, dedupe_idx, spanish_bucket)
            time.sleep(REQUEST_DELAY_SEC)
        print(f"  · [4/4] Fase Español original completada: +{len(spanish_bucket):,} obras en español", flush=True)

        # 2. Lanzamientos 2024–2025: >= 25 votos van al núcleo (in_recommender=1), < 25 votos a ampliado (in_recommender=0)
        recent_queries: list[tuple[str, dict[str, Any], str | None]] = []
        for page in range(1, 16):
            recent_queries.append((
                f"2025 p{page}",
                {"q": "first_publish_year:2025 language:eng", "sort": "want_to_read", "limit": 100, "page": page},
                None,
            ))
        for page in range(1, 16):
            recent_queries.append((
                f"2024 p{page}",
                {"q": "first_publish_year:2024 language:eng", "sort": "want_to_read", "limit": 100, "page": page},
                None,
            ))
        for subj, canon in RECENT_GENRE_QUERIES:
            for page in (1, 2):
                recent_queries.append((
                    f"2024-2025 {canon} p{page}",
                    {
                        "q": f'subject:"{subj}" first_publish_year:[2024 TO 2025] language:eng',
                        "sort": "want_to_read",
                        "limit": 100,
                        "page": page,
                    },
                    canon,
                ))

        for idx, (label, params, hint) in enumerate(recent_queries, start=1):
            if len(ext_2024_2025_bucket) >= ext_slots_needed:
                break
            docs = query_openlibrary(
                params,
                client,
                hint_genre=hint,
                expected_lang="en",
                catalog_source="openlibrary_curated",
                in_recommender=0,
            )
            for item in docs:
                y = item["publish_year"] or 0
                if y < 2024:
                    continue
                if item["ratings_count"] >= 25:
                    item["in_recommender"] = 1
                    item["catalog_source"] = "openlibrary_core"
                    _register_candidate(item, dedupe_idx, recent_voted_bucket)
                elif len(ext_2024_2025_bucket) < ext_slots_needed:
                    _register_candidate(item, dedupe_idx, ext_2024_2025_bucket)
            if idx % 10 == 0 or len(ext_2024_2025_bucket) >= ext_slots_needed:
                print(
                    f"  · [4/4] Fase 2024–2025 [{idx}/{len(recent_queries)} {label}]: "
                    f"ampliado={len(ext_2024_2025_bucket):,}/{ext_slots_needed:,} | "
                    f"promovidos >=25 votos={len(recent_voted_bucket):,}",
                    flush=True,
                )
            time.sleep(REQUEST_DELAY_SEC)

        # 3. Clásicos y autores contemporáneos faltantes + OL >= 25 votos para completar cupos del núcleo 12,000
        remaining_core = max(0, core_slots_needed - len(spanish_bucket) - len(recent_voted_bucket))
        if remaining_core > 0:
            en_queries: list[tuple[str, dict[str, Any], str | None]] = [
                (
                    f"Autor: {author_name}",
                    {"author": author_name, "language": "eng", "sort": "readinglog", "limit": 18, "page": 1},
                    hint,
                )
                for author_name, hint in NON_HISPANIC_AUTHORS
            ] + [
                (
                    f"Clásicos p{p}",
                    {"q": "subject:classics language:eng", "sort": "readinglog", "limit": 100, "page": p},
                    "Classics",
                )
                for p in range(1, 6)
            ] + [
                (
                    f"Contemporáneos 2015-2023 p{p}",
                    {"q": "first_publish_year:[2015 TO 2023] language:eng", "sort": "readinglog", "limit": 100, "page": p},
                    None,
                )
                for p in range(1, 6)
            ]
            temp_bucket: dict[str, dict[str, Any]] = {}
            target_collect = int(remaining_core * 1.4)
            for label, params, hint in en_queries:
                if len(temp_bucket) >= target_collect:
                    break
                docs = query_openlibrary(
                    params,
                    client,
                    hint_genre=hint,
                    expected_lang="en",
                    catalog_source="openlibrary_core",
                    in_recommender=1,
                )
                for item in docs:
                    _register_candidate(item, dedupe_idx, temp_bucket)
                time.sleep(REQUEST_DELAY_SEC)

            # Priorizar obras de Open Library que traen >= 25 votos reales, luego clásicos/contemporáneos
            sorted_pool = sorted(
                temp_bucket.values(),
                key=lambda b: (-(1 if b["ratings_count"] >= 25 else 0), -b["ratings_count"], -(b["publish_year"] or 0)),
            )
            classics_pool = sorted_pool[:remaining_core]
            print(
                f"  · [4/4] Fase Clásicos/Contemporáneos: +{len(classics_pool):,} libros promovidos al núcleo "
                f"(con >=25 votos: {sum(1 for b in classics_pool if b['ratings_count'] >= 25):,})",
                flush=True,
            )

    core_all = list(spanish_bucket.values()) + list(recent_voted_bucket.values()) + classics_pool
    ext_all = list(ext_2024_2025_bucket.values())

    def to_row(b: dict[str, Any]) -> tuple[Any, ...]:
        return (
            b["title"],
            b["author"],
            ", ".join(b["genres"]),
            b["primary_genre"],
            b["synopsis"],
            b["publish_year"],
            b["pages"],
            b["isbn"],
            b["avg_rating"],
            b["ratings_count"],
            b["catalog_source"],
            b["in_recommender"],
            b["external_key"],
        )

    rows = [to_row(b) for b in (core_all + ext_all)]
    inserted = 0
    if rows:
        with db_session(db_path) as conn:
            before = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO books "
                "(title, author, genres, primary_genre, synopsis, publish_year, "
                "pages, isbn, avg_rating, ratings_count, catalog_source, in_recommender, external_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            after = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
            inserted = int(after - before)

    elapsed = time.perf_counter() - t0
    print(
        f"[4/4] Enriquecimiento completado en {elapsed:.1f}s: +{inserted:,} libros insertados "
        f"(núcleo Open Library in_recommender=1: {len(core_all):,} [español={len(spanish_bucket):,}, "
        f"2024-25 >=25 votos={len(recent_voted_bucket):,}, clásicos/contemp={len(classics_pool):,}] | "
        f"ampliado 2024–2025 in_recommender=0: {len(ext_all):,})",
        flush=True,
    )
    return {
        "inserted": inserted,
        "core_added": len(core_all),
        "spanish_added": len(spanish_bucket),
        "voted_recent_added": len(recent_voted_bucket),
        "classics_added": len(classics_pool),
        "extended_added": len(ext_all),
        "elapsed_sec": round(elapsed, 2),
    }


def deduplicate_sqlite_catalog(db_path: Path | str | None = None) -> dict[str, int]:
    """Limpia in-place autores/títulos y fusiona duplicados residuales en SQLite manteniendo integridad FK."""
    init_db(db_path)
    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT book_id, title, author, synopsis, publish_year, pages, isbn, "
            "avg_rating, ratings_count, catalog_source, in_recommender "
            "FROM books"
        ).fetchall()

        books_map: dict[str, dict[str, Any]] = {}
        invalid_ids: list[int] = []
        for r in rows:
            bid = int(r["book_id"])
            ct = clean_title(r["title"])
            ca = clean_author(r["author"])
            if not ct or not ca or is_non_book_format(ct, ca) or not has_clean_punctuation(ct, ca):
                invalid_ids.append(bid)
                continue
            key = f"row_{bid}"
            books_map[key] = {
                "book_id": bid,
                "merged_ids": [bid],
                "title": ct,
                "author": ca,
                "orig_title": str(r["title"]),
                "orig_author": str(r["author"]),
                "synopsis": str(r["synopsis"] or ""),
                "publish_year": r["publish_year"],
                "pages": r["pages"],
                "isbn": r["isbn"],
                "avg_rating": r["avg_rating"],
                "ratings_count": int(r["ratings_count"] or 0),
                "catalog_source": str(r["catalog_source"] or "amazon_2023"),
                "in_recommender": int(r["in_recommender"] if r["in_recommender"] is not None else 1),
                "lang": "es" if str(r["catalog_source"]) == "openlibrary_core" else "en",
            }

        clean_map, _, merged_cnt = deduplicate_books_map(books_map)

        # Garantizar además unicidad exacta (title.lower(), author.lower()) tras clean_author
        by_ta: dict[tuple[str, str], dict[str, Any]] = {}
        final_survivors: list[dict[str, Any]] = []
        dup_pairs: list[tuple[int, int]] = []  # (dup_id, keep_id)
        for rec in sorted(
            clean_map.values(),
            key=lambda b: (-int(b["in_recommender"]), -int(b["ratings_count"]), b["book_id"]),
        ):
            ta = (rec["title"].lower(), rec["author"].lower())
            if ta in by_ta:
                keep = by_ta[ta]
                for mid in rec["merged_ids"]:
                    dup_pairs.append((mid, keep["book_id"]))
                continue
            by_ta[ta] = rec
            final_survivors.append(rec)
            keep_id = rec["book_id"]
            for mid in rec["merged_ids"]:
                if mid != keep_id:
                    dup_pairs.append((mid, keep_id))

        # 1. Migrar reading_list de duplicados hacia los registros supervivientes sin violar UNIQUE(user_id, book_id)
        for dup_id, keep_id in dup_pairs:
            conn.execute(
                "INSERT OR IGNORE INTO reading_list (user_id, book_id, status, rating, updated_at) "
                "SELECT user_id, ?, status, rating, updated_at FROM reading_list WHERE book_id = ?",
                (keep_id, dup_id),
            )
            conn.execute("DELETE FROM reading_list WHERE book_id = ?", (dup_id,))

        # 2. Eliminar filas inválidas y duplicadas antes de actualizar títulos/autores limpios
        delete_ids = [(did,) for did, _ in dup_pairs] + [(bid,) for bid in invalid_ids]
        if delete_ids:
            conn.executemany("DELETE FROM books WHERE book_id = ?", delete_ids)

        # 3. Actualizar metadatos limpios en filas supervivientes modificadas
        updated = 0
        for rec in final_survivors:
            if (
                len(rec["merged_ids"]) > 1
                or rec["title"] != rec["orig_title"]
                or rec["author"] != rec["orig_author"]
            ):
                conn.execute(
                    "UPDATE books SET title=?, author=?, synopsis=?, publish_year=?, pages=?, isbn=?, "
                    "avg_rating=?, ratings_count=?, in_recommender=? WHERE book_id=?",
                    (
                        rec["title"],
                        rec["author"],
                        rec["synopsis"],
                        rec["publish_year"],
                        rec["pages"],
                        rec["isbn"],
                        rec["avg_rating"],
                        rec["ratings_count"],
                        rec["in_recommender"],
                        rec["book_id"],
                    ),
                )
                updated += 1

    print(
        f"[Dedupe DB] filas iniciales={len(rows):,} | bloqueadas={len(invalid_ids):,} | "
        f"fusionadas={len(dup_pairs):,} | autores/títulos actualizados={updated:,} | restantes={len(final_survivors):,}",
        flush=True,
    )
    return {
        "initial": len(rows),
        "blocked": len(invalid_ids),
        "merged": len(dup_pairs),
        "updated": updated,
        "remaining": len(final_survivors),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Enriquece SQLite offline en dos niveles vía Open Library (Español + Clásicos + 2024–2025)"
    )
    ap.add_argument("--core-target", type=int, default=DEFAULT_CORE_TARGET, help="tamaño total del núcleo (default: 12000)")
    ap.add_argument("--extended-target", type=int, default=DEFAULT_EXTENDED_TARGET, help="libros recientes >2023 (default: 3000)")
    ap.add_argument("--reset-extended", action="store_true", help="reemplaza los libros previos con in_recommender=0")
    ap.add_argument("--dedupe-db", action="store_true", help="limpia autores/títulos y fusiona duplicados in-place antes de rellenar")
    args = ap.parse_args(argv)
    if args.dedupe_db:
        deduplicate_sqlite_catalog()
    enrich_database(
        core_target_total=args.core_target,
        extended_target=args.extended_target,
        reset_extended=args.reset_extended,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
