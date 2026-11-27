"""ETL en streaming: Amazon Reviews 2023 (Books 5-core, escaneo completo) + Open Library offline -> SQLite.

Uso (desde "backend/"):
    python -m app.etl_seed --reset
    python -m app.etl_seed --reset --skip-enrich

Arquitectura curada en SQLite ("booksphere_community.db", 15,000 libros):
  * Nivel 1 — Núcleo Recomendador ("in_recommender = 1", 12,000 libros):
      - Obras únicas deduplicadas por (primer autor + obra) de Amazon Books 2023 con
        "avg_rating >= 3.8★" (excluyendo "CD", "Box Set", "Coloring Book", "Workbook"
        y paquetes múltiples, y localizando obras de autores hispanohablantes al español).
      - Completado en el paso [4/4] hasta 12,000 con obras originales en español ("language:spa"),
        clásicos y autores contemporáneos faltantes y libros de Open Library con ">= 25" votos.
  * Nivel 2 — Catálogo Ampliado Reciente ("in_recommender = 0", 3,000 libros):
      - Exclusivamente lanzamientos recientes "año > 2023" (2024-2025) con "< 25" votos
        (sin alterar el recomendador ni el benchmark 80/20)."""
from __future__ import annotations

import argparse
import heapq
import json
import random
import re
import secrets
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Iterator

import httpx
import numpy as np
import pandas as pd

from .database import DATA_DIR, DB_PATH, db_session, hash_password, init_db, verify_password
from .enrich_catalog import DEFAULT_EXTENDED_TARGET, enrich_database
from .openlibrary_client import (
    canonical_work_aliases,
    canonical_work_key,
    clean_author,
    clean_text,
    clean_title,
    deduplicate_books_map,
    is_better_author,
    is_clean_en_or_es_book,
    is_cleaner_title,
    is_non_book_format,
    localize_hispanic_amazon_book,
)

# --------------------------------------------------------------------------- #
# Fuentes remotas públicas (Amazon Reviews 2023 — Books, UCSD / Hugging Face)
# --------------------------------------------------------------------------- #
META_URL = (
    "https://huggingface.co/datasets/yufan/amazon2023-item-metadata/"
    "resolve/main/Books.jsonl"
)
RATINGS_URL = (
    "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/"
    "resolve/main/benchmark/5core/rating_only/Books.csv"
)

# Parámetros del escaneo completo (0 = archivo entero hasta EOF) y curación de calidad
META_SCAN_LIMIT = 0               # 0 = escaneo completo de Books.jsonl (2.58 GB)
RATINGS_SCAN_LIMIT = 0            # 0 = escaneo completo de Books.csv (0.51 GB)
CORE_COLLAB_BOOKS = 4_200         # núcleo denso de obras únicas co-evaluadas para FC/híbrido
TARGET_CORE_TOTAL = 12_000        # objetivo final de libros en el núcleo (in_recommender=1)
AMAZON_CORE_WITH_ENRICH = 11_050  # cupo de Amazon antes del paso [4/4]
TARGET_MAX_USERS = 450            # usuarios activos de la comunidad
MIN_USER_RATINGS = 18             # actividad mínima por usuario dentro del núcleo colaborativo
MIN_BOOK_GLOBAL_RATINGS = 100     # soporte mínimo para libros previos a 2021
MIN_RECENT_GLOBAL_RATINGS = 50    # soporte mínimo más flexible para novedades 2021–2023
MIN_BOOK_AVG_RATING = 3.8         # umbral mínimo de calidad global en el núcleo Amazon
MIN_SYNOPSIS_CHARS = 60
MAX_SYNOPSIS_CHARS = 1500
N_LOGIN_USERS = 3
LOGIN_USER_MIN_BOOKS, LOGIN_USER_MAX_BOOKS = 25, 240
RANDOM_SEED = 42

STREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)

# --------------------------------------------------------------------------- #
# Taxonomía canónica de géneros (mapeada respetando la jerarquía Amazon Books)
# --------------------------------------------------------------------------- #
GENERIC_GENRES = {"Fiction", "Nonfiction"}

_DEPT_EXACT_MAP: dict[str, list[str]] = {
    "romance": ["Romance"],
    "biographies & memoirs": ["Biography & Memoir"],
    "history": ["History"],
    "children's books": ["Children's"],
    "teen & young adult": ["Young Adult"],
    "comics & graphic novels": ["Comics & Graphic Novels"],
    "cookbooks, food & wine": ["Cooking"],
    "business & money": ["Business"],
    "science & math": ["Science"],
    "computers & technology": ["Science"],
    "engineering & transportation": ["Science"],
    "medical books": ["Science"],
    "religion & spirituality": ["Religion & Spirituality"],
    "christian books & bibles": ["Religion & Spirituality"],
    "politics & social sciences": ["Politics"],
    "law": ["Politics"],
    "humor & entertainment": ["Humor"],
    "arts & photography": ["Art"],
    "travel": ["Travel"],
    "sports & outdoors": ["Sports & Outdoors"],
    "crafts, hobbies & home": ["Lifestyle & Hobbies"],
    "education & teaching": ["Education"],
    "reference": ["Education"],
    "test preparation": ["Education"],
    "self-help": ["Self Help"],
    "health, fitness & dieting": ["Self Help"],
    "parenting & relationships": ["Self Help"],
    "literature & fiction": ["Fiction"],
}

def map_amazon_categories(raw_categories: list[str]) -> list[str]:
    """Convierte el árbol jerárquico de Amazon Books en géneros canónicos limpios."""
    nodes = [str(c).strip().lower() for c in raw_categories if str(c).strip().lower() != "books"]
    if not nodes:
        return []

    dept = nodes[0]
    sub_nodes = nodes[1:]
    sub_joined = " | ".join(sub_nodes)
    found: list[str] = []

    def add(genre: str) -> None:
        if genre not in found:
            found.append(genre)

    if "horror" in sub_joined or "ghosts" in sub_joined or "occult" in sub_joined:
        add("Horror")
    if "thriller" in sub_joined or "suspense" in sub_joined:
        add("Thriller")
    if "mystery" in sub_joined or "crime" in sub_joined or "detective" in sub_joined:
        add("Mystery")
    if "romance" in sub_joined:
        add("Romance")
    if "fantasy" in sub_joined and "science fiction & fantasy" != sub_joined:
        add("Fantasy")
    if "science fiction" in sub_joined and "fantasy" not in sub_nodes:
        if any("science fiction" in n and "fantasy" not in n for n in sub_nodes):
            add("Science Fiction")
        elif "science fiction & fantasy" in sub_nodes:
            add("Fantasy")
            add("Science Fiction")
    if "action & adventure" in sub_joined:
        add("Adventure")
    if "classics" in sub_joined or "classic" in sub_joined:
        add("Classics")
    if "poetry" in sub_joined:
        add("Poetry")
    if "philosophy" in sub_joined:
        add("Philosophy")
    if "psychology" in sub_joined:
        add("Psychology")
    if "graphic novels" in sub_joined or "manga" in sub_joined or "comics" in sub_joined:
        add("Comics & Graphic Novels")
    if "memoirs" in sub_joined or "biographies" in sub_joined:
        add("Biography & Memoir")
    if "humor" in sub_joined or "satire" in sub_joined:
        add("Humor")
    if "historical fiction" in sub_joined or (
        "historical" in sub_joined
        and dept in {"literature & fiction", "romance", "teen & young adult", "children's books"}
    ):
        add("Historical Fiction")

    if dept == "science fiction & fantasy":
        if not ({"Fantasy", "Science Fiction"} & set(found)):
            add("Fantasy")
            add("Science Fiction")
    elif dept == "mystery, thriller & suspense":
        if not ({"Mystery", "Thriller"} & set(found)):
            add("Mystery")
            add("Thriller")
    elif dept in _DEPT_EXACT_MAP:
        for g in _DEPT_EXACT_MAP[dept]:
            add(g)

    specific = [g for g in found if g not in GENERIC_GENRES]
    generic = [g for g in found if g in GENERIC_GENRES]
    return specific + generic

def clean_synopsis(features: object, description: object) -> str:
    parts: list[str] = []
    if isinstance(features, list):
        parts.extend(str(p) for p in features if p)
    elif isinstance(features, str) and features.strip():
        parts.append(features)
    joined = clean_text(" ".join(parts))
    if len(joined) < 120 and description:
        desc_parts = description if isinstance(description, list) else [description]
        desc_txt = clean_text(" ".join(str(p) for p in desc_parts if p))
        desc_txt = re.sub(
            r"^(?:Review|About the Author|From the Back Cover|Product Description)\s+",
            "",
            desc_txt,
            flags=re.I,
        )
        if len(desc_txt) > len(joined):
            joined = desc_txt
    if len(joined) > MAX_SYNOPSIS_CHARS:
        joined = joined[:MAX_SYNOPSIS_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return joined

def parse_details(raw_details: object) -> tuple[str, int | None, int | None, str | None]:
    """Extrae (idioma, año exacto, páginas, isbn) del diccionario 'details' de Amazon."""
    if isinstance(raw_details, str):
        try:
            det = json.loads(raw_details)
        except json.JSONDecodeError:
            det = {}
    elif isinstance(raw_details, dict):
        det = raw_details
    else:
        det = {}

    language = str(det.get("Language", "")).strip()

    pub_year: int | None = None
    for key in ("Publication date", "Publisher"):
        val = det.get(key)
        if val:
            m = re.search(r"\b(19\d{2}|20[0-2]\d)\b", str(val))
            if m:
                y = int(m.group(1))
                if 1900 <= y <= 2024:
                    pub_year = y
                    break

    pages: int | None = None
    for v in det.values():
        if isinstance(v, str) and "page" in v.lower():
            m = re.search(r"\b(\d{2,4})\s+pages?\b", v, re.IGNORECASE)
            if m:
                pages = int(m.group(1))
                break

    isbn: str | None = None
    for key in ("ISBN 10", "ISBN-10", "ISBN 13", "ISBN-13"):
        val = det.get(key)
        if val:
            cleaned = re.sub(r"[^0-9xX]", "", str(val)).upper()
            if len(cleaned) in (10, 13):
                isbn = cleaned
                break

    return language, pub_year, pages, isbn

# --------------------------------------------------------------------------- #
# Streaming resiliente sobre HTTPS
# --------------------------------------------------------------------------- #
def iter_resilient_lines(url: str, max_retries: int = 5) -> Iterator[str]:
    """Lee líneas UTF-8 vía streaming HTTPS con reanudación automática por HTTP Range."""
    offset = 0
    buf = b""
    retries = 0

    with httpx.Client(follow_redirects=True, timeout=STREAM_TIMEOUT) as client:
        while True:
            headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
            try:
                with client.stream("GET", url, headers=headers) as resp:
                    if offset > 0 and resp.status_code == 416:
                        break
                    resp.raise_for_status()
                    if offset > 0 and resp.status_code == 200:
                        offset = 0
                        buf = b""
                    for chunk in resp.iter_bytes(chunk_size=131_072):
                        offset += len(chunk)
                        buf += chunk
                        while True:
                            idx = buf.find(b"\n")
                            if idx == -1:
                                break
                            yield buf[:idx].decode("utf-8", errors="replace")
                            buf = buf[idx + 1:]
                    if buf:
                        yield buf.decode("utf-8", errors="replace")
                        buf = b""
                    break
            except (httpx.HTTPError, OSError) as exc:
                retries += 1
                if retries > max_retries:
                    raise
                print(
                    f"  [Reconexión {retries}/{max_retries} en byte {offset:,}] {exc}",
                    flush=True,
                )
                time.sleep(2.0 * retries)

# --------------------------------------------------------------------------- #
# Paso 1: Escaneo completo de metadatos + deduplicación
# --------------------------------------------------------------------------- #
def stream_amazon_catalog(meta_limit: int) -> tuple[dict[str, dict], dict[str, str]]:
    """Escanea Books.jsonl (completo si meta_limit==0), deduplica por obra/primer autor y localiza español."""
    t0 = time.perf_counter()
    scope = "COMPLETO (2.58 GB)" if meta_limit <= 0 else f"hasta {meta_limit:,} registros"
    print(f"[1/4] Escaneando metadatos en streaming ({scope}) desde Amazon Reviews 2023...", flush=True)

    books_by_key: dict[str, dict] = {}
    alias_to_canon: dict[str, str] = {}
    asin_to_key: dict[str, str] = {}
    scanned = 0
    filtered_low_score = 0
    filtered_format = 0
    localized_es = 0

    def reached_limit() -> bool:
        return meta_limit > 0 and scanned >= meta_limit

    for line in iter_resilient_lines(META_URL):
        if not line:
            continue
        scanned += 1
        if scanned % 100_000 == 0:
            print(
                f"  · [1/4] {scanned:,} registros escaneados -> "
                f"{len(books_by_key):,} obras únicas (formato descartado={filtered_format:,}, "
                f"<3.8★={filtered_low_score:,}, español={localized_es:,}, {time.perf_counter() - t0:.1f}s)",
                flush=True,
            )

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if reached_limit():
                break
            continue

        asin = str(obj.get("parent_asin") or "").strip()
        if not asin:
            if reached_limit():
                break
            continue

        lang, pub_year, pages, isbn = parse_details(obj.get("details"))
        if lang != "English":
            if reached_limit():
                break
            continue

        r_num = int(obj.get("rating_number") or 0)
        avg_r = float(obj.get("average_rating") or 0.0)
        min_r_needed = MIN_RECENT_GLOBAL_RATINGS if (pub_year or 0) >= 2021 else MIN_BOOK_GLOBAL_RATINGS
        if r_num < min_r_needed or avg_r < MIN_BOOK_AVG_RATING:
            if avg_r < MIN_BOOK_AVG_RATING:
                filtered_low_score += 1
            if reached_limit():
                break
            continue

        genres = map_amazon_categories(obj.get("categories") or [])
        if not genres:
            if reached_limit():
                break
            continue

        raw_title = str(obj.get("title") or "")
        raw_author = str(obj.get("store") or "")
        if is_non_book_format(raw_title, raw_author):
            filtered_format += 1
            if reached_limit():
                break
            continue

        title = clean_title(raw_title)
        author = clean_author(raw_author)
        if is_non_book_format(title, author):
            filtered_format += 1
            if reached_limit():
                break
            continue
        synopsis = clean_synopsis(obj.get("features"), obj.get("description"))

        # Localizar obras originalmente en español
        loc = localize_hispanic_amazon_book(title, author, genres, pub_year)
        target_lang = "en"
        if loc is not None:
            es_title, es_author, es_syn = loc
            if not es_title:
                # Traducción inglesa sin equivalente directo -> omitir; Open Library aportará español
                if reached_limit():
                    break
                continue
            title = es_title
            author = es_author
            if es_syn:
                synopsis = es_syn
            target_lang = "es"
            localized_es += 1

        if len(synopsis) < MIN_SYNOPSIS_CHARS or not is_clean_en_or_es_book(title, author, synopsis, expected_lang=target_lang):
            if reached_limit():
                break
            continue

        canon_key = canonical_work_key(title, author)
        if not canon_key:
            if reached_limit():
                break
            continue

        aliases = canonical_work_aliases(title, author) or [canon_key]
        matched_key = next((alias_to_canon[al] for al in aliases if al in alias_to_canon), None)
        target_key = matched_key or canon_key

        # Todas las ediciones suman hacia target_key
        asin_to_key[asin] = target_key
        for al in aliases:
            alias_to_canon.setdefault(al, target_key)

        existing = books_by_key.get(target_key)
        if existing is None:
            books_by_key[target_key] = {
                "canon_key": target_key,
                "parent_asin": asin,
                "title": title,
                "author": author,
                "genres": genres,
                "primary_genre": genres[0],
                "synopsis": synopsis,
                "publish_year": pub_year,
                "pages": pages,
                "isbn": isbn,
                "avg_rating": round(avg_r, 2),
                "ratings_count": r_num,
                "lang": target_lang,
            }
        else:
            # Fusionar metadatos: mayor ratings_count + título más limpio + autor completo sin iniciales sueltas
            if r_num > existing["ratings_count"]:
                existing["ratings_count"] = r_num
                existing["avg_rating"] = round(avg_r, 2)
                existing["parent_asin"] = asin
                if len(synopsis) > len(existing["synopsis"]):
                    existing["synopsis"] = synopsis
            if target_lang == "es" or (
                existing["lang"] == target_lang and is_cleaner_title(title, existing["title"])
            ):
                existing["title"] = title
            if is_better_author(author, existing["author"]):
                existing["author"] = author
            if existing["publish_year"] is None and pub_year is not None:
                existing["publish_year"] = pub_year
            if existing["pages"] is None and pages is not None:
                existing["pages"] = pages
            if existing["isbn"] is None and isbn is not None:
                existing["isbn"] = isbn

        if reached_limit():
            break

    books_by_key, asin_to_key, layer2_merged = deduplicate_books_map(books_by_key, asin_to_key)
    elapsed = time.perf_counter() - t0
    print(
        f"[1/4] Catálogo escaneado en {elapsed:.1f}s: {scanned:,} registros -> "
        f"{len(books_by_key):,} obras únicas limpias ({len(asin_to_key):,} parent_asins fusionados, "
        f"+{layer2_merged:,} fusiones Capa 2 | descartados formato={filtered_format:,}, "
        f"<{MIN_BOOK_AVG_RATING}★={filtered_low_score:,})",
        flush=True,
    )
    return books_by_key, asin_to_key


# --------------------------------------------------------------------------- #
# Paso 2: Escaneo completo 5-Core en O(1) RAM
# --------------------------------------------------------------------------- #
def stream_amazon_ratings(
    books_by_key: dict[str, dict],
    asin_to_key: dict[str, str],
    ratings_limit: int,
) -> pd.DataFrame:
    """Escanea Books.csv y selecciona los 450 usuarios más densos sobre núcleo de obras únicas."""
    t0 = time.perf_counter()
    scope = "COMPLETO (0.51 GB)" if ratings_limit <= 0 else f"hasta {ratings_limit:,} líneas"
    print(f"[2/4] Escaneando calificaciones 5-core ({scope}) sobre núcleo colaborativo de {CORE_COLLAB_BOOKS:,} obras...", flush=True)

    by_pop = sorted(books_by_key.values(), key=lambda b: (-b["ratings_count"], -b["avg_rating"]))
    core_keys_list = [b["canon_key"] for b in by_pop[:3_500]]
    core_set = set(core_keys_list)
    recent_pool = [b for b in by_pop if (b["publish_year"] or 0) >= 2020 and b["canon_key"] not in core_set]
    for b in recent_pool[: CORE_COLLAB_BOOKS - len(core_keys_list)]:
        core_keys_list.append(b["canon_key"])
        core_set.add(b["canon_key"])

    key_to_idx = {k: i for i, k in enumerate(core_keys_list)}
    core_asin_to_idx = {
        asin: key_to_idx[key]
        for asin, key in asin_to_key.items()
        if key in key_to_idx
    }

    heap: list[tuple[int, int, str, dict[int, float]]] = []
    cur_uid: str | None = None
    cur_dict: dict[int, tuple[int, float]] = {}
    block_seq = 0
    scanned = 0
    matched = 0

    def flush_user(uid_str: str | None, items: dict[int, tuple[int, float]]) -> None:
        nonlocal block_seq
        if not uid_str or len(items) < MIN_USER_RATINGS:
            return
        block_seq += 1
        compact = {k_idx: float(rat) for k_idx, (_ts, rat) in items.items()}
        entry = (len(compact), -block_seq, uid_str, compact)
        if len(heap) < TARGET_MAX_USERS:
            heapq.heappush(heap, entry)
        elif entry > heap[0]:
            heapq.heapreplace(heap, entry)

    it = iter_resilient_lines(RATINGS_URL)
    next(it, None)
    for line in it:
        if not line:
            continue
        scanned += 1
        if scanned % 2_500_000 == 0:
            print(
                f"  · [2/4] {scanned:,} ratings escaneados ({matched:,} cruces en núcleo, "
                f"heap={len(heap)} usuarios, {time.perf_counter() - t0:.1f}s)",
                flush=True,
            )

        parts = line.split(",")
        if len(parts) < 4:
            if ratings_limit > 0 and scanned >= ratings_limit:
                break
            continue

        uid = parts[0]
        if uid != cur_uid:
            flush_user(cur_uid, cur_dict)
            cur_uid = uid
            cur_dict = {}

        k_idx = core_asin_to_idx.get(parts[1])
        if k_idx is not None:
            try:
                rat = float(parts[2])
                ts = int(parts[3])
            except ValueError:
                rat = 0.0
                ts = 0
            if 1.0 <= rat <= 5.0:
                prev = cur_dict.get(k_idx)
                if prev is None or ts >= prev[0]:
                    cur_dict[k_idx] = (ts, float(round(rat)))
                    matched += 1

        if ratings_limit > 0 and scanned >= ratings_limit:
            break

    flush_user(cur_uid, cur_dict)

    selected = sorted(heap, key=lambda e: (-e[0], e[2]))
    rows: list[tuple[int, str, float]] = []
    for seq_uid, (_n_it, _neg_seq, _raw_uid, compact) in enumerate(selected, start=1):
        for k_idx, rat in compact.items():
            rows.append((seq_uid, core_keys_list[k_idx], rat))

    df_ratings = pd.DataFrame(rows, columns=["user_id", "canon_key", "rating"])
    elapsed = time.perf_counter() - t0
    print(
        f"[2/4] Ratings escaneados en {elapsed:.1f}s: {scanned:,} líneas -> "
        f"{matched:,} cruces exactos en núcleo | seleccionados: "
        f"{df_ratings['user_id'].nunique():,} usuarios, {len(df_ratings):,} interacciones, "
        f"{df_ratings['canon_key'].nunique():,} obras únicas co-evaluadas",
        flush=True,
    )
    return df_ratings

# --------------------------------------------------------------------------- #
# Paso 3: Selección del Núcleo Amazon, Cuentas Disjuntas y Carga SQLite
# --------------------------------------------------------------------------- #
def top_genres(book_genres: list[list[str]], n: int = 3) -> list[str]:
    c = Counter(g for gl in book_genres for g in gl if g not in GENERIC_GENRES)
    if not c:
        c = Counter(g for gl in book_genres for g in gl)
    return [g for g, _ in c.most_common(n)]

def select_diverse_login_users(
    eligible: list[int],
    fav: dict[int, list[str]],
    n_books: pd.Series,
) -> list[int]:
    """Selecciona 3 cuentas activas reales cuyos Top-3 géneros favoritos sean disjuntos (0 solapamiento)."""
    rich = [u for u in eligible if len(fav.get(u, [])) >= 3]
    best_triple: list[int] | None = None
    best_score = -1.0

    for i, u1 in enumerate(rich):
        s1 = set(fav[u1][:3])
        for j in range(i + 1, len(rich)):
            u2 = rich[j]
            s2 = set(fav[u2][:3])
            if s1 & s2:
                continue
            for k in range(j + 1, len(rich)):
                u3 = rich[k]
                s3 = set(fav[u3][:3])
                if (s1 & s3) or (s2 & s3):
                    continue
                score = float(min(n_books[u1], n_books[u2], n_books[u3]) * 3 + n_books[u1] + n_books[u2] + n_books[u3])
                if score > best_score:
                    best_score = score
                    best_triple = [u1, u2, u3]

    if best_triple:
        return best_triple

    chosen: list[int] = []
    taken: set[str] = set()
    for u in eligible:
        if fav[u] and fav[u][0] not in taken:
            chosen.append(u)
            taken.add(fav[u][0])
        if len(chosen) == N_LOGIN_USERS:
            break
    for u in eligible:
        if len(chosen) == N_LOGIN_USERS:
            break
        if u not in chosen:
            chosen.append(u)
    return chosen

def build_and_populate(
    books_by_key: dict[str, dict],
    df_ratings: pd.DataFrame,
    amazon_target_size: int = AMAZON_CORE_WITH_ENRICH,
) -> list[dict]:
    rng = random.Random(RANDOM_SEED)

    local_counts = df_ratings["canon_key"].value_counts().to_dict() if not df_ratings.empty else {}
    rated_keys_sorted = sorted(
        local_counts.keys(),
        key=lambda k: (-local_counts[k], -books_by_key[k]["ratings_count"]),
    )
    keep_keys: list[str] = list(rated_keys_sorted[:amazon_target_size])
    keep_set = set(keep_keys)

    if len(keep_keys) < amazon_target_size:
        unrated = [b for k, b in books_by_key.items() if k not in keep_set]

        def quality_pop_key(b: dict) -> tuple[float, int, str]:
            # Puntaje bayesiano combinado con log(votos) para priorizar obras de alta calidad y soporte
            bayes = (b["ratings_count"] / (b["ratings_count"] + 500.0)) * b["avg_rating"] + (
                500.0 / (b["ratings_count"] + 500.0)
            ) * 4.3
            score = bayes * 0.65 + np.log1p(b["ratings_count"]) * 0.25
            return (-float(score), -b["ratings_count"], b["title"])

        # 1. Asegurar las obras en español localizadas con ratings de Amazon
        es_books = sorted([b for b in unrated if b.get("lang") == "es"], key=quality_pop_key)
        for b in es_books:
            if len(keep_keys) >= amazon_target_size:
                break
            keep_keys.append(b["canon_key"])
            keep_set.add(b["canon_key"])

        # 2. Novedades 2021–2023 mejor valoradas (hasta 1,400)
        recent = sorted(
            [b for b in unrated if (b["publish_year"] or 0) >= 2021 and b["canon_key"] not in keep_set],
            key=quality_pop_key,
        )
        for b in recent[:1400]:
            if len(keep_keys) >= amazon_target_size:
                break
            keep_keys.append(b["canon_key"])
            keep_set.add(b["canon_key"])

        # 3. Variedad equilibrada por género clave (mantiene cola larga temáticamente diversa)
        priority_genres = (
            "Fantasy", "Romance", "Horror", "Science Fiction", "Mystery",
            "Thriller", "Classics", "Biography & Memoir", "History", "Young Adult",
        )
        for g_name in priority_genres:
            g_pool = sorted(
                [b for b in unrated if b["canon_key"] not in keep_set and b["primary_genre"] == g_name],
                key=quality_pop_key,
            )
            for b in g_pool[:320]:
                if len(keep_keys) >= amazon_target_size:
                    break
                keep_keys.append(b["canon_key"])
                keep_set.add(b["canon_key"])

        # 4. Completar con las obras únicas mejor puntuadas y más valoradas globalmente
        rest = sorted(
            [b for b in unrated if b["canon_key"] not in keep_set],
            key=quality_pop_key,
        )
        for b in rest:
            if len(keep_keys) >= amazon_target_size:
                break
            keep_keys.append(b["canon_key"])
            keep_set.add(b["canon_key"])

    selected_books = sorted(
        [books_by_key[k] for k in keep_keys],
        key=lambda b: (-b["ratings_count"], b["title"]),
    )
    seen_ta: set[tuple[str, str]] = set()
    final_books: list[dict] = []
    for b in selected_books:
        ta = (b["title"].lower(), b["author"].lower())
        if ta in seen_ta:
            continue
        seen_ta.add(ta)
        final_books.append(b)

    id_by_key = {b["canon_key"]: idx for idx, b in enumerate(final_books, start=1)}
    genres_by_id = {idx: b["genres"] for idx, b in enumerate(final_books, start=1)}

    df_r = df_ratings[df_ratings["canon_key"].isin(id_by_key)].copy()
    df_r["book_id"] = df_r["canon_key"].map(id_by_key)
    u_counts = df_r["user_id"].value_counts()
    valid_uids = sorted(u_counts[u_counts >= 10].index.tolist())
    df_r = df_r[df_r["user_id"].isin(valid_uids)].copy()

    uid_remap = {old: new for new, old in enumerate(valid_uids, start=1)}
    df_r["user_id"] = df_r["user_id"].map(uid_remap)

    fav: dict[int, list[str]] = {}
    for uid, g in df_r.groupby("user_id"):
        liked = g[g["rating"] >= 4.0]["book_id"].map(genres_by_id).tolist()
        fav[int(uid)] = top_genres(liked or g["book_id"].map(genres_by_id).tolist())

    n_books_s = df_r["user_id"].value_counts()
    eligible = [
        int(u) for u in n_books_s.index
        if LOGIN_USER_MIN_BOOKS <= int(n_books_s[u]) <= LOGIN_USER_MAX_BOOKS and fav.get(int(u))
    ]
    if len(eligible) < N_LOGIN_USERS:
        eligible = [int(u) for u in n_books_s.index if fav.get(int(u))]

    login_ids = select_diverse_login_users(eligible, fav, n_books_s)
    if len(login_ids) < N_LOGIN_USERS:
        raise RuntimeError("No hay suficientes usuarios activos para habilitar el login de prueba.")

    credentials: list[dict] = []
    login_info: dict[int, tuple[str, str]] = {}
    for u in login_ids:
        username = f"lector_{u}"
        password = secrets.token_urlsafe(9)
        login_info[u] = (username, password)
        credentials.append({
            "user_id": u,
            "username": username,
            "password": password,
            "favorite_genres": fav[u],
            "n_books": int(n_books_s[u]),
        })

    book_rows = [
        (
            idx,
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
            "amazon_2023",
            1,
            b["parent_asin"],
        )
        for idx, b in enumerate(final_books, start=1)
    ]

    user_rows = []
    for u in sorted(fav):
        username, password = login_info.get(u, (f"lector_{u}", None))
        user_rows.append((
            u,
            username,
            hash_password(password) if password else None,
            f"Lector #{u}",
            ", ".join(fav[u]),
            1,
        ))

    list_rows = []
    for u, g in df_r.groupby("user_id"):
        uid_int = int(u)
        entries = [[uid_int, int(b), "leido", float(r)] for b, r in zip(g["book_id"], g["rating"])]
        if uid_int in login_info:
            pool = [e for e in entries if 3.0 <= e[3] <= 5.0]
            rng.shuffle(pool)
            for e in pool[:2]:
                e[2], e[3] = "leyendo", None
            for e in pool[2:5]:
                e[2], e[3] = "por_leer", None
        list_rows.extend(tuple(e) for e in entries)

    print(
        f"[3/4] Guardando {len(book_rows):,} obras únicas Amazon Books 2023, usuarios e interacciones...",
        flush=True,
    )
    with db_session() as conn:
        conn.executemany(
            "INSERT INTO books (book_id,title,author,genres,primary_genre,synopsis,publish_year,"
            "pages,isbn,avg_rating,ratings_count,catalog_source,in_recommender,external_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            book_rows,
        )
        conn.executemany(
            "INSERT INTO users (user_id,username,password_hash,display_name,favorite_genres,"
            "is_seed_user) VALUES (?,?,?,?,?,?)",
            user_rows,
        )
        conn.executemany(
            "INSERT INTO reading_list (user_id,book_id,status,rating) VALUES (?,?,?,?)",
            list_rows,
        )
    return credentials

def write_credentials(credentials: list[dict]) -> Path:
    path = DATA_DIR / "seed_credentials.private.txt"
    lines = [
        "# CREDENCIALES PRIVADAS - no compartir ni versionar",
        "# Cuentas reales (Amazon Reviews 2023 - Books) con login habilitado para probar 'usuario activo'",
        "",
    ]
    for c in credentials:
        lines += [
            f"usuario    : {c['username']}",
            f"contraseña : {c['password']}",
            f"perfil     : {', '.join(c['favorite_genres'])} ({c['n_books']} libros)",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

def report(credentials: list[dict]) -> None:
    with db_session() as conn:
        one = lambda q: conn.execute(q).fetchone()[0]
        print("\n=== RESUMEN FINAL DE LA BASE DE DATOS (Curada, Deduplicada y Bilingüe EN/ES) ===")
        n_books = one("SELECT COUNT(*) FROM books")
        n_core = one("SELECT COUNT(*) FROM books WHERE in_recommender = 1")
        n_ext = one("SELECT COUNT(*) FROM books WHERE in_recommender = 0")
        n_amz = one("SELECT COUNT(*) FROM books WHERE catalog_source = 'amazon_2023'")
        n_ol_core = one("SELECT COUNT(*) FROM books WHERE catalog_source = 'openlibrary_core'")
        n_ol_ext = one("SELECT COUNT(*) FROM books WHERE catalog_source = 'openlibrary_curated'")
        n_unrated = one("SELECT COUNT(*) FROM books WHERE avg_rating IS NULL")
        n_users = one("SELECT COUNT(*) FROM users")
        n_inter = one("SELECT COUNT(*) FROM reading_list")
        n_rated_books = one("SELECT COUNT(DISTINCT book_id) FROM reading_list")
        sparsity = (1.0 - n_inter / max(n_users * n_core, 1)) * 100
        print(
            f"catálogo total: {n_books:,} libros (in_recommender=1: {n_core:,} [Amazon={n_amz:,}, OL_core={n_ol_core:,}] | "
            f"in_recommender=0: {n_ext:,} [OL_2024_2025={n_ol_ext:,}] | sin puntuación global={n_unrated:,})"
        )
        print(
            f"matriz colaborativa: {n_users:,} usuarios | {n_inter:,} interacciones | "
            f"{n_rated_books:,} obras co-evaluadas (dispersión núcleo {sparsity:.2f}%)"
        )
        print(
            f"años exactos: {one('SELECT COUNT(*) FROM books WHERE publish_year IS NOT NULL'):,}/{n_books:,} "
            f"(rango {one('SELECT MIN(publish_year) FROM books')}–{one('SELECT MAX(publish_year) FROM books')}, "
            f"2021–2023: {one('SELECT COUNT(*) FROM books WHERE publish_year BETWEEN 2021 AND 2023'):,}, "
            f"2024–2026: {one('SELECT COUNT(*) FROM books WHERE publish_year >= 2024'):,})"
        )
        print(
            "integridad:", one("PRAGMA integrity_check"),
            "| violaciones FK:", len(conn.execute("PRAGMA foreign_key_check").fetchall()),
            "| doble '::':", one("SELECT COUNT(*) FROM books WHERE title LIKE '%::%' OR author LIKE '%::%'"),
            "| Amazon <3.8★:", one("SELECT COUNT(*) FROM books WHERE catalog_source='amazon_2023' AND avg_rating < 3.8"),
        )
        print("\nLibros por género principal:")
        for r in conn.execute("SELECT primary_genre g, COUNT(*) n FROM books GROUP BY g ORDER BY n DESC"):
            print(f"  {r['g']:<26}{r['n']:>6}")
        print("\nCuentas con login habilitado (perfiles disjuntos):")
        for c in credentials:
            st = {
                r["status"]: r["n"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) n FROM reading_list WHERE user_id=? GROUP BY status",
                    (c["user_id"],),
                )
            }
            row = conn.execute("SELECT password_hash FROM users WHERE user_id=?", (c["user_id"],)).fetchone()
            ok = verify_password(c["password"], row["password_hash"]) and not verify_password("x", row["password_hash"])
            print(f"  {c['username']:<14} perfil={', '.join(c['favorite_genres'])} estados={st} hash_ok={ok}")

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ETL Streaming Amazon Reviews 2023 + Enriquecimiento Offline Open Library")
    ap.add_argument("--reset", action="store_true", help="borra y reconstruye la base de datos local")
    ap.add_argument("--meta-limit", type=int, default=META_SCAN_LIMIT, help="límite de metadatos (0 = escaneo completo)")
    ap.add_argument("--ratings-limit", type=int, default=RATINGS_SCAN_LIMIT, help="límite de ratings (0 = completo)")
    ap.add_argument("--skip-enrich", action="store_true", help="omite el enriquecimiento offline con Open Library")
    ap.add_argument("--enrich-target", type=int, default=DEFAULT_EXTENDED_TARGET, help="libros 2024–2025 adicionales (default: 3000)")
    args = ap.parse_args(argv)

    if DB_PATH.exists():
        if not args.reset:
            print(f"Ya existe {DB_PATH}. Use --reset para reconstruirla (se perderán los usuarios registrados).")
            return 1
        DB_PATH.unlink()
    init_db()

    amazon_size = TARGET_CORE_TOTAL if args.skip_enrich else AMAZON_CORE_WITH_ENRICH
    books_by_key, asin_to_key = stream_amazon_catalog(args.meta_limit)
    df_ratings = stream_amazon_ratings(books_by_key, asin_to_key, args.ratings_limit)
    creds = build_and_populate(books_by_key, df_ratings, amazon_target_size=amazon_size)
    path = write_credentials(creds)
    if not args.skip_enrich and args.enrich_target > 0:
        enrich_database(core_target_total=TARGET_CORE_TOTAL, extended_target=args.enrich_target)
    report(creds)
    print(f"\nBase de datos local: {DB_PATH}\nCredenciales privadas: {path}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
