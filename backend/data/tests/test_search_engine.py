"""Pruebas del motor de búsqueda.

Ejecutar desde "backend/":
    python -m unittest discover -s tests -t . -v

* "SyntheticCatalogTests": BD temporal con libros inventados -> comportamiento determinista.
* "RealCatalogTests": catálogo real (se omiten si aún no se ejecutó el ETL)."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.database import DB_PATH, db_session, init_db
from app.openlibrary_client import (
    CatalogDedupeIndex,
    canonical_work_aliases,
    clean_author,
    deduplicate_books_map,
    has_clean_punctuation,
    is_non_book_format,
)
from app.search_engine import BookSearchEngine, normalize_text

# (title, author, genres, primary, synopsis, year, avg_rating, ratings_count)
SYNTHETIC = [
    ("Wizard School", "Ann Smith", "Fantasy, Young Adult", "Fantasy",
     "A young boy discovers magic and a castle full of secrets.", 1997, 4.5, 50000),
    ("The Castle of Wizards", "Bob Jones", "Fantasy", "Fantasy",
     "Several wizards fight over a throne.", 2003, 4.0, 8000),
    ("Cooking With Fire", "Carla Diaz", "Cooking", "Cooking",
     "Recipes where the wizard of the grill reveals his secrets.", 2010, 4.2, 3000),
    ("Cien años de soledad", "Gabriel García Márquez", "Fiction, Classics", "Classics",
     "La historia de la familia Buendía en Macondo.", 1967, 4.7, 90000),
    ("Murder at Dawn", "Dana Lee", "Mystery, Thriller", "Mystery",
     "A detective investigates a murder in a small town.", 1999, 3.2, 40),
    ("Murder on the Lake", "Dana Lee", "Mystery", "Mystery",
     "Another murder case for the same detective.", 2005, 4.6, 12000),
    ("Dark Tales", "Eve Poe", "Horror", "Horror",
     "Short stories about ghosts and haunted houses.", None, 4.1, 700),
    ("Tiny Hit", "Fay Ng", "Romance", "Romance",
     "Two strangers meet on a train.", 2015, 5.0, 3),
]

def build_synthetic_db(path: Path) -> None:
    init_db(path)
    with db_session(path) as conn:
        conn.executemany(
            "INSERT INTO books (title,author,genres,primary_genre,synopsis,publish_year,avg_rating,ratings_count)"
            " VALUES (?,?,?,?,?,?,?,?)", SYNTHETIC)

class SyntheticCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        db = Path(cls._tmp.name) / "synthetic.db"
        build_synthetic_db(db)
        cls.engine = BookSearchEngine(db)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def titles(self, **kw) -> list[str]:
        return [r["title"] for r in self.engine.search(**kw)["results"]]

    def test_title_match_outranks_synopsis_match(self):
        # "wizard": título (x3) > sinopsis (x1); el libro de cocina solo lo menciona en la sinopsis
        t = self.titles(query="wizard")
        self.assertEqual(t[0], "Wizard School")
        self.assertLess(t.index("Wizard School"), t.index("Cooking With Fire"))

    def test_plural_stemming(self):
        self.assertIn("The Castle of Wizards", self.titles(query="wizard"))
        self.assertIn("Wizard School", self.titles(query="wizards"))

    def test_accent_insensitive(self):
        self.assertEqual(self.titles(query="garcia marquez")[0], "Cien años de soledad")
        self.assertEqual(self.titles(query="CIEN ANOS")[0], "Cien años de soledad")
        self.assertEqual(self.titles(query="la familia buendia en macondo")[0], "Cien años de soledad")
        self.assertEqual(normalize_text("García-Márquez!"), "garcia marquez")

    def test_typo_tolerance(self):
        self.assertEqual(self.titles(query="marqes")[0], "Cien años de soledad")

    def test_exact_mode_is_strict(self):
        self.assertEqual(self.engine.search(query="marqes", exact=True)["total"], 0)
        self.assertEqual(self.titles(query="wizard school", exact=True), ["Wizard School"])
        # límite de palabra: "wizard" exacto no es "wizards"
        self.assertEqual(self.titles(query="wizard", exact=True), ["Wizard School"])

    def test_filters_combined(self):
        r = self.engine.search(query="murder", genre="Mystery", min_rating=4.0)
        self.assertEqual([x["title"] for x in r["results"]], ["Murder on the Lake"])

    def test_rating_sort_is_bayesian(self):
        # "Tiny Hit" (5.0 con 3 votos) no debe superar a libros con muchos votos y buena nota
        order = self.titles(sort_by="rating")
        self.assertNotEqual(order[0], "Tiny Hit")
        self.assertLess(order.index("Cien años de soledad"), order.index("Tiny Hit"))
        for r in self.engine.search(sort_by="rating")["results"]:
            lo, hi = sorted((r["avg_rating"], self.engine.prior_c))
            self.assertTrue(lo - 1e-6 <= r["weighted_rating"] <= hi + 1e-6)

    def test_popularity_sort(self):
        counts = [r["ratings_count"] for r in self.engine.search(sort_by="popularity")["results"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_unknown_genre_and_case_insensitive_genre(self):
        self.assertEqual(self.engine.search(genre="Nope")["total"], 0)
        self.assertEqual(self.engine.search(genre="fantasy")["total"], 2)

    def test_short_or_garbage_query(self):
        self.assertEqual(self.engine.search(query="a")["total"], 0)
        self.assertEqual(self.engine.search(query="zxqwvbn")["total"], 0)

    def test_invalid_parameters(self):
        with self.assertRaises(ValueError):
            self.engine.search(sort_by="alphabetical")
        with self.assertRaises(ValueError):
            self.engine.search(limit=0)
        with self.assertRaises(ValueError):
            self.engine.search(limit=1000)
        with self.assertRaises(ValueError):
            self.engine.search(offset=-1)

    def test_get_book_and_genres(self):
        b = self.engine.get_book(1)
        self.assertEqual(b["title"], "Wizard School")
        self.assertIn("synopsis", b)
        self.assertIsNone(self.engine.get_book(9999))
        counts = {g["genre"]: g["count"] for g in self.engine.list_genres()}
        self.assertEqual(counts["Fantasy"], 2)
        self.assertEqual(counts["Mystery"], 2)

    def test_unrated_extended_books_searchable_without_nan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "ext_search.db"
            build_synthetic_db(db)
            with db_session(db) as conn:
                # Insertar varios libros ampliados 2025 sin calificaciones (>25% del catálogo)
                ext_rows = [
                    (f"Onyx Horizon {i}", "Rebecca Yarros", "Fantasy, Romance", "Fantasy",
                     "A brand new 2025 fantasy novel about dragon riders fighting at midnight.",
                     2025, None, 0, "openlibrary_curated", 0, f"/works/OL900{i}W")
                    for i in range(1, 7)
                ]
                conn.executemany(
                    "INSERT INTO books (title,author,genres,primary_genre,synopsis,publish_year,"
                    "avg_rating,ratings_count,catalog_source,in_recommender,external_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ext_rows,
                )
            eng = BookSearchEngine(db)
            self.assertGreater(eng.prior_m, 0.0)
            res = eng.search(query="Onyx Horizon", limit=10)
            self.assertEqual(res["total"], 6)
            self.assertFalse(res["results"][0]["in_recommender"])
            self.assertIsNone(res["results"][0]["avg_rating"])
            self.assertIsNone(res["results"][0]["weighted_rating"])
            # Al filtrar por calificación mínima, libros sin puntuación quedan fuera correctamente
            filtered = eng.search(query="Onyx Horizon", min_rating=3.0)
            self.assertEqual(filtered["total"], 0)


class CleaningAndDedupeTests(unittest.TestCase):
    def test_author_initials_and_formatting_standardized(self):
        cases = {
            "C S Lewis": "C. S. Lewis",
            "C.S. Lewis": "C. S. Lewis",
            "George R R Martin": "George R. R. Martin",
            "George R.R. Martin": "George R. R. Martin",
            "R F Kuang": "R. F. Kuang",
            "J.K. Rowling": "J. K. Rowling",
            "Rowling J K": "J. K. Rowling",
            "Martin George R R": "George R. R. Martin",
            "E L James": "E. L. James",
            "Sarah J Maas": "Sarah J. Maas",
            "Viktor E Frankl": "Viktor E. Frankl",
            "J. R. R Tolkien": "J. R. R. Tolkien",
            "K a Tucker": "K. A. Tucker",
            "Malcolm X": "Malcolm X",
            "Dr. Seuss": "Dr. Seuss",
            "Follett, Ken": "Ken Follett",
            "Smith, Patrick D.": "Patrick D. Smith",
            "Tozer, A. W.": "A. W. Tozer",
            "ONE, Yusuke Murata": "ONE, Yusuke Murata",
            "Flea, Patti Smith": "Flea, Patti Smith",
            "Julia Quinn Julia Quinn": "Julia Quinn",
            "JOY ELLIS": "Joy Ellis",
            "HARARI YUVAL NOAH": "Yuval Noah Harari",
            "Dr. Joseph Murphy Ph.D.": "Joseph Murphy",
            "Brené Brown Ph.D. L.M.S.W.": "Brené Brown",
            "L. M. S. W. Brené Brown, Ph.D., Lauren Fortgang": "Brené Brown, Lauren Fortgang",
            "MD Gabor Maté, Peter A. Levine": "Gabor Maté, Peter A. Levine",
            "Tina Payne Bryson PhD Daniel J. Siegel, M. D.": "Tina Payne Bryson Daniel J. Siegel",
            "Rowling J": "J. K. Rowling",
            "José Ortega y Gasset": "José Ortega y Gasset",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_author(raw), expected)

    def test_layer1_and_layer2_dedupe_merges_true_duplicates(self):
        idx = CatalogDedupeIndex()
        idx.add("Verity", "Colleen Hoover")
        self.assertTrue(idx.is_duplicate("Verity: The thriller that will capture your heart", "Colleen Hoover"))

        idx.add("Fool Me Once", "Harlan Coben")
        self.assertTrue(idx.is_duplicate("Fool Me Once: From the #1 bestselling creator of the hit Netflix series Stay Close", "Harlan Coben"))

        idx.add("American Gods", "Neil Gaiman")
        self.assertTrue(idx.is_duplicate("American Gods: Author's Preferred Text", "Neil Gaiman"))

        idx.add("Harry Potter and the Deathly Hallows", "J. K. Rowling, Mary Grandpre")
        self.assertTrue(idx.is_duplicate("Harry Potter and the Deathly Hallows (Harry Potter, Book 7): Volume 7", "J. K. Rowling"))

        idx.add("The Cuckoo's Calling (Cormoran Strike)", "Robert Galbraith (AKA J. K. Rowling)")
        self.assertTrue(idx.is_duplicate("The Cuckoo's Calling", "J. K. Rowling"))

        idx.add("Mistborn (The Mistborn Saga, 1)", "Sanderson")
        self.assertTrue(idx.is_duplicate("Mistborn: The Final Empire", "Brandon Sanderson"))

        idx.add("The Tattooist of Auschwitz: A Novel", "Heather Morris")
        self.assertTrue(idx.is_duplicate("The Tattooist of Auschwitz: Based on an incredible true story", "Heather Morris"))

        idx.add("Pride and Prejudice", "Jane Austen")
        self.assertTrue(idx.is_duplicate("Pride & Prejudice (Pulp! The Classics)", "Jane Austen"))

        idx.add("Alice's Adventures in Wonderland", "Lewis Carroll")
        self.assertTrue(idx.is_duplicate("Alices Adventures in Wonderland", "Lewis Carroll"))

        idx.add("The Assassin's Blade", "Sarah J. Maas")
        self.assertTrue(idx.is_duplicate("The Assassin’s Blade (Miniature Character Edition)", "Sarah J Maas"))

        idx.add("Sapiens: A Brief History of Humankind", "Yuval Harari")
        self.assertTrue(idx.is_duplicate("Sapiens", "Yuval Noah Harari"))

        idx.add("Good Omens", "Neil Gaiman, Terry Pratchett")
        self.assertTrue(idx.is_duplicate("Good Omens", "Terry Pratchett, Neil Gaiman"))
        self.assertTrue(idx.is_duplicate("Good Omens", "N. Gaiman"))

        idx.add("All the Colors of the Dark", "Chris Whitaker")
        self.assertTrue(idx.is_duplicate("All the Colours of the Dark", "Chris Whitaker"))

        idx.add("The Outsiders 50th Anniversary Edition", "S. E. Hinton")
        self.assertTrue(idx.is_duplicate("The Outsiders 40th Anniversary edition", "S. E. Hinton"))

        # Artículos iniciales detectados en alias sin mutilar título real
        self.assertTrue(set(canonical_work_aliases("The Help", "Kathryn Stockett")) & set(canonical_work_aliases("Help", "Kathryn Stockett")))

    def test_series_and_distinct_authors_never_merged(self):
        idx = CatalogDedupeIndex()
        idx.add("Lore Olympus: Volume One", "Rachel Smythe")
        self.assertFalse(idx.is_duplicate("Lore Olympus: Volume Two", "Rachel Smythe"))

        idx.add("The 5th Wave", "Rick Yancey")
        idx.add("The 5th Wave: The Infinite Sea (Book 2)", "Rick Yancey")
        self.assertFalse(idx.is_duplicate("The 5th Wave: The Last Star (Book 3)", "Rick Yancey"))

        idx.add("Dog Man: Brawl of the Wild", "Dav Pilkey")
        self.assertFalse(idx.is_duplicate("Dog Man: Lord of the Fleas", "Dav Pilkey"))

        idx.add("Star Wars: Heir to the Empire", "Timothy Zahn")
        self.assertFalse(idx.is_duplicate("Star Wars: Dark Force Rising", "Timothy Zahn"))

        idx.add("The Stranger", "Albert Camus")
        self.assertFalse(idx.is_duplicate("The Stranger", "Harlan Coben"))

    def test_author_typo_merge_and_non_book_blocking(self):
        books = {
            "a": {"canon_key": "a", "title": "Twelve Years a Slave", "author": "Solomon Northup", "ratings_count": 500, "avg_rating": 4.6, "lang": "en", "synopsis": "Memoir"},
            "b": {"canon_key": "b", "title": "Twelve Years a Slave", "author": "Solomon Northrup", "ratings_count": 50, "avg_rating": 4.5, "lang": "en", "synopsis": "Memoir"},
        }
        clean_b, _, merged = deduplicate_books_map(books)
        self.assertEqual(len(clean_b), 1)
        self.assertEqual(merged, 1)

        self.assertTrue(is_non_book_format("School Zone - Multiplication 0-12 Flash Cards", "School Zone"))
        self.assertTrue(is_non_book_format("World of Eric Carle Animal Sound Book", "PI Kids"))
        self.assertTrue(is_non_book_format("Paint by Sticker Kids: Zoo Animals", "Workman Publishing"))
        self.assertFalse(has_clean_punctuation("Born A Crime: Stories from a South African Childhood", "Born A Crime: Stories from a South African Childhood"))


@unittest.skipUnless(DB_PATH.exists(), "Base real no encontrada: ejecute 'python -m app.etl_seed --reset'")
class RealCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = BookSearchEngine()

    def search(self, **kw):
        return self.engine.search(**kw)

    def test_exact_title_first(self):
        # Toma un título popular real del catálogo (Amazon Books 2023) y valida búsqueda exacta y estándar
        sample = next(
            b for b in self.search(sort_by="popularity", limit=20)["results"]
            if 2 <= len(b["title"].split()) <= 5 and ":" not in b["title"] and "(" not in b["title"]
        )
        top = self.search(query=sample["title"], limit=3)["results"]
        self.assertTrue(
            any(r["book_id"] == sample["book_id"] for r in top),
            f"{sample['title']!r} no apareció en top-3: {[r['title'] for r in top]}",
        )
        exact = self.search(query=sample["title"], exact=True, limit=5)["results"]
        self.assertTrue(any(r["book_id"] == sample["book_id"] for r in exact))

    def test_author_search_and_typo_tolerance(self):
        with db_session() as conn:
            row = conn.execute(
                "SELECT author FROM books "
                "WHERE author NOT LIKE '%,%' AND length(author) BETWEEN 8 AND 24 "
                "GROUP BY author HAVING COUNT(*) >= 5 "
                "ORDER BY SUM(ratings_count) DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        author = row["author"]
        last_name = author.split()[-1]

        top = self.search(query=author.lower(), limit=5)["results"]
        self.assertTrue(top and all(last_name.lower() in r["author"].lower() for r in top))

        # Introducir 1 error tipográfico en el apellido del autor (eliminar una letra interior)
        if len(last_name) >= 5:
            typo_last = last_name[:2] + last_name[3:]
            typo_query = f"{author.split()[0]} {typo_last}"
            typo_top = self.search(query=typo_query, limit=3)["results"]
            self.assertTrue(
                typo_top and any(last_name.lower() in r["author"].lower() for r in typo_top),
                f"{typo_query!r} -> {[r['author'] for r in typo_top]}",
            )

    def test_catalog_quality_clean_english_and_recent_years(self):
        import re
        from app.openlibrary_client import canonical_work_key, is_non_book_format
        non_latin = re.compile(r"[\u0400-\u052F\u0600-\u06FF\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]")
        bad_edge = re.compile(r"^[><~*•·|=+_#@]|[><~*•·|=+_#@]$")
        with db_session() as conn:
            blobs = conn.execute(
                "SELECT COUNT(*) FROM books WHERE typeof(title)='blob' OR typeof(author)='blob' OR typeof(synopsis)='blob'"
            ).fetchone()[0]
            self.assertEqual(blobs, 0)
            core_cnt = conn.execute("SELECT COUNT(*) FROM books WHERE in_recommender = 1").fetchone()[0]
            ext_cnt = conn.execute("SELECT COUNT(*) FROM books WHERE in_recommender = 0").fetchone()[0]
            self.assertGreaterEqual(core_cnt, 11500)
            self.assertGreaterEqual(ext_cnt, 2500)
            recent_24_25 = conn.execute("SELECT COUNT(*) FROM books WHERE publish_year >= 2024").fetchone()[0]
            self.assertGreater(recent_24_25, 2500, f"Se esperaban >2500 libros 2024–2025, encontrados: {recent_24_25}")
            old_ext = conn.execute(
                "SELECT COUNT(*) FROM books "
                "WHERE in_recommender = 0 AND catalog_source = 'openlibrary_curated' "
                "AND (publish_year IS NULL OR publish_year < 2024)"
            ).fetchone()[0]
            self.assertEqual(
                old_ext,
                0,
                "Todo libro reciente de Open Library (openlibrary_curated, in_recommender=0) debe ser >=2024",
            )
            low_amz = conn.execute(
                "SELECT COUNT(*) FROM books WHERE catalog_source = 'amazon_2023' AND avg_rating < 3.8"
            ).fetchone()[0]
            self.assertEqual(low_amz, 0)
            empty_syn = conn.execute("SELECT COUNT(*) FROM books WHERE synopsis IS NULL OR length(trim(synopsis)) < 35").fetchone()[0]
            self.assertEqual(empty_syn, 0)

            # Español original para autores hispanohablantes (cero traducciones inglesas)
            es_gabo = conn.execute(
                "SELECT COUNT(*) FROM books WHERE lower(title) LIKE '%cien años de soledad%'"
            ).fetchone()[0]
            en_gabo = conn.execute(
                "SELECT COUNT(*) FROM books WHERE lower(title) LIKE '%one hundred years of solitude%' "
                "OR lower(title) LIKE '%love in the time of cholera%'"
            ).fetchone()[0]
            self.assertGreaterEqual(es_gabo, 1)
            self.assertEqual(en_gabo, 0)

            seen_canon: set[str] = set()
            no_dot_init_re = re.compile(r"(?:^|\s)[A-Z](?:\s+[A-Z])+(?=\s+[A-Z][a-z])|\b[A-Z][a-z]+\s+[A-Z]\s+[A-Z][a-z]+")
            glued_init_re = re.compile(r"\b[A-Z]\.[A-Z]\.?")
            for r in conn.execute("SELECT title, author FROM books"):
                t, a = r["title"], r["author"]
                self.assertNotIn("::", t, f"Doble dos puntos en título: {t!r}")
                self.assertNotIn("::", a, f"Doble dos puntos en autor: {a!r}")
                self.assertIsNone(bad_edge.search(t), f"Símbolo anómalo en título: {t!r}")
                self.assertIsNone(bad_edge.search(a), f"Símbolo anómalo en autor: {a!r}")
                self.assertIsNone(non_latin.search(t), f"Título no latino detectado: {t!r}")
                self.assertIsNone(non_latin.search(a), f"Autor no latino detectado: {a!r}")
                self.assertIsNone(no_dot_init_re.search(a), f"Iniciales de autor sin punto: {a!r}")
                self.assertIsNone(glued_init_re.search(a), f"Iniciales pegadas sin espacio: {a!r}")
                self.assertFalse(is_non_book_format(t, a), f"Formato no-libro detectado: {t!r}")
                ck = canonical_work_key(t, a)
                self.assertNotIn(ck, seen_canon, f"Obra duplicada detectada: {t!r} ({a!r})")
                seen_canon.add(ck)

    def test_all_filters_respected(self):
        r = self.search(query="murder", genre="Mystery", min_rating=3.8, max_rating=4.8, limit=100)
        self.assertGreater(r["total"], 0)
        for x in r["results"]:
            self.assertIn("Mystery", x["genres"])
            self.assertTrue(3.8 <= x["avg_rating"] <= 4.8)

    def test_sorting(self):
        rel = [r["relevance"] for r in self.search(query="love story", limit=50)["results"]]
        self.assertEqual(rel, sorted(rel, reverse=True))
        pop = [r["ratings_count"] for r in self.search(genre="Fantasy", sort_by="popularity", limit=50)["results"]]
        self.assertEqual(pop, sorted(pop, reverse=True))
        wr = [r["weighted_rating"] for r in self.search(genre="Fantasy", sort_by="rating", limit=50)["results"]]
        self.assertEqual(wr, sorted(wr, reverse=True))

    def test_pagination_is_consistent(self):
        full = self.search(query="dragon", limit=40)["results"]
        pages = self.search(query="dragon", limit=20, offset=0)["results"] \
            + self.search(query="dragon", limit=20, offset=20)["results"]
        self.assertEqual([r["book_id"] for r in full], [r["book_id"] for r in pages])

    def test_genre_counts_match_filter_totals(self):
        for g in self.engine.list_genres()[:5]:
            self.assertEqual(self.search(genre=g["genre"], limit=1)["total"], g["count"])

    def test_no_query_returns_whole_catalog(self):
        self.assertEqual(self.search(limit=1)["total"], self.engine.n)

    def test_latency_under_requirement(self):
        queries = ["love", "dragon", "war", "the girl", "stephen king", "murder mystery", "history of"]
        t0 = time.perf_counter()
        for q in queries * 3:
            self.search(query=q, limit=20)
        avg = (time.perf_counter() - t0) / (len(queries) * 3)
        self.assertLess(avg, 0.25, f"{avg*1000:.0f} ms/consulta (RNF-04 exige < 1500 ms)")

if __name__ == "__main__":
    unittest.main()
