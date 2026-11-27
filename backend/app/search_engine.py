"""Motor de búsqueda de Recuperación de Información.

Ranking de relevancia (consulta "q" vs. libro "i"):
    Score(q, i) = 0.55 * BM25(q, i)        # coincidencia léxica ponderada por campo
                + 0.25 * cos_char(q, i)    # n-gramas de caracteres: tolera typos/parciales
                + 0.20 * substring(q, i)   # bono por subcadena literal en título/autor

* BM25 se calcula sobre un tf ponderado por campo (título x3, autor x2, géneros x2,
  sinopsis x1), con k1=1.5 y b=0.75, y se normaliza por el máximo de cada consulta.
* "cos_char" es el coseno TF-IDF de n-gramas (3-4) de caracteres sobre "título + autor".
* Modo "exact=True": solo frases completas (límite de palabra) en título o autor.

Ordenamientos: "relevance" | "popularity" (nº de calificaciones) |
"rating" (media bayesiana ponderada, que penaliza libros con pocos votos)."""
from __future__ import annotations

import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, CountVectorizer, TfidfVectorizer

from .database import get_connection

# Hiperparámetros del ranking
W_BM25, W_CHAR, W_SUBSTR = 0.55, 0.25, 0.20
FIELD_WEIGHTS = {"title": 3.0, "author": 2.0, "genres": 2.0, "synopsis": 1.0}
BM25_K1, BM25_B = 1.5, 0.75
CHAR_NGRAM_RANGE = (3, 4)
CHAR_MIN_SIM = 0.30          # por debajo, la similitud de n-gramas se considera ruido
FUZZY_MIN_TOKEN_LEN = 4      # corrección de typos solo en palabras de 4+ letras
FUZZY_MIN_SIM = 0.30         # similitud mínima de n-gramas con la palabra candidata
FUZZY_TOP_K = 5              # candidatos de corrección por palabra
FUZZY_MAX_DF = 2             # solo se corrigen palabras que aparecen en <=2 libros (o en ninguno)
MIN_RELEVANCE = 0.10         # relevancia mínima para aparecer en los resultados
PRIOR_PERCENTILE = 0.25      # soporte mínimo m de la media bayesiana (percentil de ratings_count)

SORT_OPTIONS = ("relevance", "popularity", "rating")
MIN_QUERY_CHARS = 2          # consultas más cortas no son informativas: no devuelven resultados
MAX_LIMIT = 100
SNIPPET_CHARS = 220

SPANISH_STOP_WORDS = frozenset({
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al",
    "en", "y", "e", "o", "u", "a", "por", "para", "con", "sin", "sobre",
    "su", "sus", "que", "es", "son", "se", "lo", "le", "les", "mi", "tu",
})
COMBINED_STOP_WORDS = ENGLISH_STOP_WORDS | SPANISH_STOP_WORDS

# --------------------------------------------------------------------------- #
# Normalización de texto
# --------------------------------------------------------------------------- #
def normalize_text(text: object) -> str:
    """Minúsculas, sin acentos ni puntuación, espacios colapsados."""
    s = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()

def _stem(token: str) -> str:
    """Stemmer mínimo de plurales ingleses (wizards -> wizard, stories -> story)."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token

def analyze(text: object) -> list[str]:
    return [_stem(t) for t in normalize_text(text).split()]

# --------------------------------------------------------------------------- #
# Motor
# --------------------------------------------------------------------------- #
class BookSearchEngine:
    """Índice en memoria del catálogo; se construye una vez y atiende consultas en milisegundos."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self.reload()

    # build
    def reload(self) -> None:
        with self._lock:
            conn = get_connection(self._db_path)
            try:
                df = pd.read_sql_query(
                    "SELECT book_id, title, author, genres, primary_genre, synopsis, publish_year, "
                    "pages, isbn, avg_rating, ratings_count, "
                    "COALESCE(catalog_source, 'amazon_2023') AS catalog_source, "
                    "COALESCE(in_recommender, 1) AS in_recommender "
                    "FROM books ORDER BY book_id", conn)
            finally:
                conn.close()
            if df.empty:
                raise RuntimeError("La tabla 'books' está vacía: ejecute primero 'python -m app.etl_seed --reset'.")
            df["synopsis"] = df["synopsis"].fillna("")
            self.df = df
            self.n = len(df)
            self.book_ids = df["book_id"].to_numpy()
            self._row_of_id = {int(b): i for i, b in enumerate(self.book_ids)}

            self.ratings = df["avg_rating"].to_numpy(dtype="float64")
            self.counts = df["ratings_count"].to_numpy(dtype="float64")

            # Géneros: máscara booleana por género (un libro pertenece a varios)
            self._genre_lists = [[g.strip() for g in s.split(",") if g.strip()] for s in df["genres"]]
            self._genre_masks: dict[str, np.ndarray] = {}
            for i, gl in enumerate(self._genre_lists):
                for g in gl:
                    self._genre_masks.setdefault(g, np.zeros(self.n, dtype=bool))[i] = True
            self._genre_lookup = {normalize_text(g): g for g in self._genre_masks}

            # Media bayesiana ponderada (IMDb) calculada solo sobre libros valorados;
            # los libros sin puntuación (avg_rating=NULL / count=0) reciben score 0.0 (sin NaN).
            rated_mask = (~np.isnan(self.ratings)) & (self.counts > 0)
            pos_counts = self.counts[rated_mask]
            self.prior_m = float(np.quantile(pos_counts, PRIOR_PERCENTILE)) if len(pos_counts) > 0 else 10.0
            self.prior_m = max(self.prior_m, 1.0)
            self.prior_c = float(np.mean(self.ratings[rated_mask])) if np.any(rated_mask) else 3.5

            wr = np.zeros(self.n, dtype=np.float64)
            if np.any(rated_mask):
                v = self.counts[rated_mask]
                r_val = self.ratings[rated_mask]
                wr[rated_mask] = (v / (v + self.prior_m)) * r_val + (self.prior_m / (v + self.prior_m)) * self.prior_c
            self.weighted = wr

            self._build_bm25(df)
            self._build_char_index(df)

            self._title_norm = [normalize_text(t) for t in df["title"]]
            self._author_norm = [normalize_text(a) for a in df["author"]]
            self._title_pad = [f" {t} " for t in self._title_norm]
            self._author_pad = [f" {a} " for a in self._author_norm]

    def _build_bm25(self, df: pd.DataFrame) -> None:
        fields = {"title": df["title"], "author": df["author"],
                  "genres": df["genres"], "synopsis": df["synopsis"]}
        cv = CountVectorizer(analyzer=analyze, dtype=np.float64)
        cv.fit(pd.concat(fields.values(), ignore_index=True))
        tf = sum(FIELD_WEIGHTS[name] * cv.transform(col) for name, col in fields.items()).tocsr()

        doc_len = np.asarray(tf.sum(axis=1)).ravel()
        avgdl = doc_len.mean()
        doc_freq = np.asarray((tf > 0).sum(axis=0)).ravel()
        self._df = doc_freq
        idf = np.log(1.0 + (self.n - doc_freq + 0.5) / (doc_freq + 0.5))

        coo = tf.tocoo()
        denom = coo.data + BM25_K1 * (1.0 - BM25_B + BM25_B * doc_len[coo.row] / avgdl)
        weights = idf[coo.col] * coo.data * (BM25_K1 + 1.0) / denom
        self._bm25 = sparse.csc_matrix((weights, (coo.row, coo.col)), shape=tf.shape)
        self._vocab = cv.vocabulary_

        # Vocabulario de nombres (palabras de títulos y autores) para corregir typos por término
        words = sorted({t for col in (df["title"], df["author"]) for s in col for t in analyze(s)
                        if len(t) >= FUZZY_MIN_TOKEN_LEN})
        self._name_words = words
        self._name_vec = TfidfVectorizer(analyzer="char_wb", ngram_range=CHAR_NGRAM_RANGE,
                                         dtype=np.float32)
        self._name_mat = self._name_vec.fit_transform(words).tocsr()

    def _fuzzy_terms(self, token: str) -> list[tuple[int, float]]:
        """Términos del vocabulario parecidos a "token" (typos): [(columna BM25, similitud)].
        Solo se corrigen palabras desconocidas o casi inexistentes en el catálogo; exige la misma
        letra inicial y longitud parecida para no inventar coincidencias."""
        if len(token) < FUZZY_MIN_TOKEN_LEN:
            return []
        col = self._vocab.get(token)
        if col is not None and self._df[col] > FUZZY_MAX_DF:
            return []
        sims = (self._name_mat @ self._name_vec.transform([token]).T).toarray().ravel()
        out: list[tuple[int, float]] = []
        for j in np.argsort(-sims)[:FUZZY_TOP_K]:
            w = self._name_words[j]
            if (sims[j] < FUZZY_MIN_SIM or w == token or w[0] != token[0]
                    or abs(len(w) - len(token)) > 3):
                continue
            c = self._vocab.get(w)
            if c is not None:
                out.append((c, float(sims[j])))
        return out

    def _build_char_index(self, df: pd.DataFrame) -> None:
        """Un índice de n-gramas por campo (título / autor): un autor corto no se diluye en el título."""
        self._char_vec: dict[str, TfidfVectorizer] = {}
        self._char_mat: dict[str, sparse.csr_matrix] = {}
        for name in ("title", "author"):
            vec = TfidfVectorizer(analyzer="char_wb", ngram_range=CHAR_NGRAM_RANGE,
                                  preprocessor=normalize_text, sublinear_tf=True, dtype=np.float32)
            self._char_mat[name] = vec.fit_transform(df[name]).tocsr()
            self._char_vec[name] = vec

    # scoring
    def _relevance_scores(self, q: str) -> np.ndarray:
        # 1) BM25 normalizado por el máximo de la consulta. Si hay términos informativos,
        #    se ignoran las stopwords en inglés y español ("history of" -> "history", "cien anos de soledad").
        tokens = analyze(q)
        content = [t for t in tokens if t not in COMBINED_STOP_WORDS] or tokens
        term_w: dict[int, float] = {}
        for t in content:
            if t in self._vocab:
                term_w[self._vocab[t]] = 1.0
            for col, sim in self._fuzzy_terms(t):          # typos: aporte proporcional a la similitud
                term_w[col] = max(term_w.get(col, 0.0), sim)
        bm25 = np.zeros(self.n)
        if term_w:
            cols = list(term_w)
            raw = np.asarray(self._bm25[:, cols] @ np.array([term_w[c] for c in cols])).ravel()
            if raw.max() > 0:
                bm25 = raw / raw.max()
        # 2) Coseno de n-gramas de caracteres: máximo entre título y autor (tolera typos y parciales)
        sim = np.zeros(self.n)
        for name in ("title", "author"):
            s = (self._char_mat[name] @ self._char_vec[name].transform([q]).T).toarray().ravel()
            sim = np.maximum(sim, s)
        char = np.where(sim >= CHAR_MIN_SIM, sim, 0.0)
        # 3) Frase literal con límite de palabra en título (1.0) o autor (0.8)
        pq = f" {q} "
        substr = np.fromiter(
            (1.0 if pq in t else (0.8 if pq in a else 0.0)
             for t, a in zip(self._title_pad, self._author_pad)),
            dtype=np.float64, count=self.n)
        score = W_BM25 * bm25 + W_CHAR * char + W_SUBSTR * substr
        return np.where(score >= MIN_RELEVANCE, score, 0.0)

    def _exact_scores(self, q: str) -> np.ndarray:
        """Frase completa en título o autor; sin tolerancia a errores."""
        pq = f" {q} "
        out = np.zeros(self.n)
        for i in range(self.n):
            t = self._title_norm[i]
            if t == q:
                out[i] = 1.0
            elif t.startswith(q + " "):
                out[i] = 0.9
            elif pq in self._title_pad[i]:
                out[i] = 0.8
            elif pq in self._author_pad[i]:
                out[i] = 0.7
        return out

    # filtros
    def _filter_mask(self, genre: str | None, min_rating: float | None, max_rating: float | None) -> np.ndarray:
        mask = np.ones(self.n, dtype=bool)
        if genre:
            canon = self._genre_lookup.get(normalize_text(genre))
            mask &= self._genre_masks[canon] if canon else np.zeros(self.n, dtype=bool)
        # Las comparaciones con NaN dan False: un libro sin dato queda fuera si se filtra por él.
        if min_rating is not None:
            mask &= self.ratings >= min_rating
        if max_rating is not None:
            mask &= self.ratings <= max_rating
        return mask

    # API
    def search(self, query: str = "", genre: str | None = None,
               min_rating: float | None = None, max_rating: float | None = None,
               sort_by: str = "relevance", exact: bool = False,
               limit: int = 20, offset: int = 0) -> dict[str, Any]:
        if sort_by not in SORT_OPTIONS:
            raise ValueError(f"sort_by debe ser uno de {SORT_OPTIONS}")
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit debe estar entre 1 y {MAX_LIMIT}")
        if offset < 0:
            raise ValueError("offset no puede ser negativo")

        mask = self._filter_mask(genre, min_rating, max_rating)
        q = normalize_text(query)
        scores = None
        if q:
            if len(q) < MIN_QUERY_CHARS:
                scores = np.zeros(self.n)
            else:
                scores = self._exact_scores(q) if exact else self._relevance_scores(q)
            mask &= scores > 0

        idx = np.flatnonzero(mask)
        if scores is not None and sort_by == "relevance":
            primary = scores[idx]
            secondary = self.weighted[idx]
        elif sort_by == "rating":
            primary, secondary = self.weighted[idx], self.counts[idx]
        else:   # popularity, o relevance sin consulta (no hay relevancia que ordenar)
            primary, secondary = self.counts[idx], self.weighted[idx]
        # lexsort: última clave = principal; book_id ascendente como desempate estable
        order = np.lexsort((self.book_ids[idx], -secondary, -primary))
        page = idx[order][offset:offset + limit]

        return {
            "query": query, "total": int(len(idx)), "limit": limit, "offset": offset,
            "sort_by": sort_by, "exact": exact,
            "results": [self._row(i, None if scores is None else float(scores[i])) for i in page],
        }

    def _row(self, i: int, relevance: float | None, full: bool = False) -> dict[str, Any]:
        r = self.df.iloc[i]
        synopsis = r["synopsis"]
        has_rating = (not pd.isna(r["avg_rating"])) and int(r["ratings_count"]) > 0
        row = {
            "book_id": int(r["book_id"]), "title": r["title"], "author": r["author"],
            "genres": self._genre_lists[i], "primary_genre": r["primary_genre"],
            "publish_year": None if pd.isna(r["publish_year"]) else int(r["publish_year"]),
            "pages": None if pd.isna(r["pages"]) else int(r["pages"]),
            "avg_rating": None if pd.isna(r["avg_rating"]) else float(r["avg_rating"]),
            "ratings_count": int(r["ratings_count"]),
            "weighted_rating": round(float(self.weighted[i]), 3) if has_rating else None,
            "catalog_source": str(r["catalog_source"]),
            "in_recommender": bool(int(r["in_recommender"])),
            "relevance": None if relevance is None else round(relevance, 4),
        }
        if full:
            row["synopsis"], row["isbn"] = synopsis, (None if pd.isna(r["isbn"]) else str(r["isbn"]))
        else:
            row["synopsis_snippet"] = (synopsis if len(synopsis) <= SNIPPET_CHARS
                                       else synopsis[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…")
        return row

    def get_book(self, book_id: int) -> dict[str, Any] | None:
        i = self._row_of_id.get(int(book_id))
        return None if i is None else self._row(i, None, full=True)

    def list_genres(self) -> list[dict[str, Any]]:
        """Géneros del catálogo con nº de libros que lo incluyen y nº que lo tienen como principal."""
        primary = self.df["primary_genre"].value_counts().to_dict()
        out = [{"genre": g, "count": int(m.sum()), "primary_count": int(primary.get(g, 0))}
               for g, m in self._genre_masks.items()]
        return sorted(out, key=lambda d: (-d["count"], d["genre"]))

# --------------------------------------------------------------------------- #
# Instancia compartida (la usará FastAPI)
# --------------------------------------------------------------------------- #
_engine: BookSearchEngine | None = None
_lock = threading.Lock()

def get_engine() -> BookSearchEngine:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = BookSearchEngine()
    return _engine
