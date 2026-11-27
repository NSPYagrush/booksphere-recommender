"""Pruebas unitarias e integración del Motor Recomendador Híbrido en 3 Niveles (Fase 3).

Ejecutar desde ``backend/``:
    python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.database import DB_PATH, db_session, hash_password, init_db
from app.recommender import (
    IMPLICIT_RATING_BY_STATUS,
    HybridBookRecommender,
    effective_rating,
)

# Catálogo sintético con 3 clústeres claros (Sci-Fi, Mystery, Romance) + 1 libro sin ratings (Long Tail)
SYNTHETIC_BOOKS = [
    # id 1..4: Science Fiction
    ("Star Voyage", "Arthur Clarke", "Science Fiction, Fiction", "Science Fiction",
     "Deep space exploration aboard a starship toward distant alien galaxies.", 1968, 4.5, 50000),
    ("Galactic Empire", "Isaac Asimov", "Science Fiction, Classics", "Science Fiction",
     "A galactic empire collapses while mathematicians predict the future of planets.", 1951, 4.6, 80000),
    ("Mars Colony", "Andy Weir", "Science Fiction, Adventure", "Science Fiction",
     "An astronaut survives alone on Mars using science and orbital spacecraft engineering.", 2014, 4.4, 90000),
    ("Quantum Stars", "Greg Egan", "Science Fiction", "Science Fiction",
     "Quantum physics and distant starship voyages across unexplored alien galaxies.", 2018, 4.2, 1200),
    # id 5..7: Mystery / Thriller
    ("Murder on the Train", "Agatha Christie", "Mystery, Thriller", "Mystery",
     "A brilliant detective investigates a secret murder inside a snowbound express train.", 1934, 4.5, 75000),
    ("Shadow Detective", "Arthur Conan Doyle", "Mystery, Classics", "Mystery",
     "Detective clues reveal a clever crime conspiracy in foggy Victorian London.", 1892, 4.4, 65000),
    ("The Locked Room", "Tana French", "Mystery, Thriller", "Mystery",
     "A cold case murder mystery with suspects hiding dark forensic clues.", 2012, 4.1, 15000),
    # id 8..9: Romance
    ("Summer in Paris", "Emily Henry", "Romance, Fiction", "Romance",
     "Two strangers fall in love during a warm summer romance in Paris cafes.", 2021, 4.3, 45000),
    ("Letters of Love", "Jane Austen", "Romance, Classics", "Romance",
     "Romantic letters, courtship, marriage and love across English countryside estates.", 1813, 4.6, 120000),
]


def build_synthetic_db(path: Path) -> None:
    init_db(path)
    with db_session(path) as conn:
        conn.executemany(
            "INSERT INTO books (title,author,genres,primary_genre,synopsis,publish_year,avg_rating,ratings_count)"
            " VALUES (?,?,?,?,?,?,?,?)",
            SYNTHETIC_BOOKS,
        )
        # Usuarios:
        # 1: fan Sci-Fi (activo)
        # 2: vecino idéntico a 1 en Sci-Fi (co-evalúa libros 1,2,5 y recomienda libro 3 con 5★)
        # 3: vecino cercano a 1 en Sci-Fi (co-evalúa libros 1,2,5 y recomienda libro 3 con 5★)
        # 4: lector que solo comparte 1 libro con usuario 1 (debe descartarse con min_overlap>=2)
        # 5: usuario recién registrado en Cold Start (0 libros, favorito = Mystery)
        # 6: fan Mystery
        users = [
            (1, "scifi_user", hash_password("pw"), "SciFi User", "Science Fiction", 0),
            (2, "neighbor_exact", None, "Neighbor Exact", "Science Fiction", 1),
            (3, "neighbor_close", None, "Neighbor Close", "Science Fiction", 1),
            (4, "single_overlap", None, "Single Overlap", "Romance", 1),
            (5, "cold_mystery", hash_password("pw"), "Cold Mystery", "Mystery", 0),
            (6, "mystery_fan", None, "Mystery Fan", "Mystery", 1),
        ]
        conn.executemany(
            "INSERT INTO users (user_id,username,password_hash,display_name,favorite_genres,is_seed_user)"
            " VALUES (?,?,?,?,?,?)",
            users,
        )
        interactions = [
            # Usuario 1: libros 1 (5★), 2 (4★), 5 (2★)
            (1, 1, "leido", 5.0),
            (1, 2, "leido", 4.0),
            (1, 5, "leido", 2.0),
            # Usuario 2: idéntico a 1 en {1,2,5} -> MSD=0, sim_msd_norm=1.0; además calificó libro 3 con 5★
            (2, 1, "leido", 5.0),
            (2, 2, "leido", 4.0),
            (2, 5, "leido", 2.0),
            (2, 3, "leido", 5.0),
            # Usuario 3: difiere en 1★ en cada común -> diffs=[1,1,1] -> MSD=1.0, Cheb=1.0; libro 3 con 5★
            (3, 1, "leido", 4.0),
            (3, 2, "leido", 3.0),
            (3, 5, "leido", 3.0),
            (3, 3, "leido", 5.0),
            # Usuario 4: solo coincide en libro 1 (5★), NO debe entrar al vecindario por RNF-05 (overlap=1 < 2)
            (4, 1, "leido", 5.0),
            (4, 8, "leido", 5.0),
            (4, 9, "leido", 5.0),
            # Usuario 6: fan Mystery
            (6, 5, "leido", 5.0),
            (6, 6, "leido", 5.0),
            (6, 7, "leido", 4.0),
        ]
        conn.executemany(
            "INSERT INTO reading_list (user_id,book_id,status,rating) VALUES (?,?,?,?)",
            interactions,
        )


class SyntheticRecommenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "synthetic_reco.db"
        build_synthetic_db(self.db_path)
        self.rec = HybridBookRecommender(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_implicit_rating_rules_rf10(self):
        self.assertEqual(IMPLICIT_RATING_BY_STATUS["por_leer"], 3.0)
        self.assertEqual(IMPLICIT_RATING_BY_STATUS["leyendo"], 3.7)
        self.assertEqual(IMPLICIT_RATING_BY_STATUS["leido"], 4.0)
        self.assertEqual(effective_rating("por_leer", None), 3.0)
        self.assertEqual(effective_rating("leyendo", None), 3.7)
        self.assertEqual(effective_rating("leido", None), 4.0)
        # Explícita siempre prevalece sobre el estado
        self.assertEqual(effective_rating("por_leer", 5.0), 5.0)
        self.assertEqual(effective_rating("leyendo", 2.0), 2.0)

    def test_exact_msd_chebyshev_and_no_zero_imputation_rnf05(self):
        neighbors = self.rec.compute_user_neighbors(1, min_overlap=2)
        by_uid = {n["user_id"]: n for n in neighbors}

        # Usuario 4 solo comparte 1 libro -> debe ser descartado estrictamente (RNF-05)
        self.assertNotIn(4, by_uid)
        self.assertIn(2, by_uid)
        self.assertIn(3, by_uid)

        # Usuario 2: intersección {1,2,5} idéntica -> MSD = 0.0, Chebyshev = 0.0, sim_msd_norm = 1.0
        # Si NaN en libro 3 se hubiera imputado con 0, MSD habría sido > 0!
        self.assertEqual(by_uid[2]["overlap_books"], 3)
        self.assertAlmostEqual(by_uid[2]["msd_value"], 0.0, places=4)
        self.assertAlmostEqual(by_uid[2]["chebyshev_dist"], 0.0, places=4)
        self.assertAlmostEqual(by_uid[2]["sim_msd_norm"], 1.0, places=4)
        self.assertAlmostEqual(by_uid[2]["sim_msd_hyp"], 1.0, places=4)

        # Usuario 3: diffs = |5-4|=1, |4-3|=1, |2-3|=1 -> MSD = 1.0, Cheb = 1.0, sim_msd_norm = 1 - 1/16 = 0.9375
        self.assertEqual(by_uid[3]["overlap_books"], 3)
        self.assertAlmostEqual(by_uid[3]["msd_value"], 1.0, places=4)
        self.assertAlmostEqual(by_uid[3]["chebyshev_dist"], 1.0, places=4)
        self.assertAlmostEqual(by_uid[3]["sim_msd_norm"], 0.9375, places=4)
        self.assertAlmostEqual(by_uid[3]["sim_msd_hyp"], 0.5, places=4)

    def test_cold_start_and_live_reactivity_rf11_rnf06(self):
        # Usuario 5: 0 libros en lista, género favorito = Mystery
        cold = self.rec.recommend_for_home(5, top_n=5)
        self.assertEqual(cold["mode"], "cold_start")
        self.assertEqual(cold["alpha"], 0.0)
        top_genres = [x["primary_genre"] for x in cold["recommendations"][:3]]
        self.assertTrue(all(g == "Mystery" for g in top_genres), f"Cold Start no priorizó Mystery: {top_genres}")

        # Ahora el usuario 5 agrega dos libros sin calificación explícita (estados por_leer=3.0 y leyendo=3.7)
        with db_session(self.db_path) as conn:
            conn.execute("INSERT INTO reading_list (user_id, book_id, status, rating) VALUES (5, 5, 'leyendo', NULL)")
            conn.execute("INSERT INTO reading_list (user_id, book_id, status, rating) VALUES (5, 6, 'por_leer', NULL)")
        self.rec.sync_user_row(5)

        eff = self.rec.get_user_effective_ratings(5)
        self.assertEqual(eff[5], 3.7)
        self.assertEqual(eff[6], 3.0)

        # Inmediatamente debe pasar a modo híbrido y excluir libros 5 y 6 ya agregados
        active = self.rec.recommend_for_home(5, top_n=5)
        self.assertEqual(active["mode"], "hybrid")
        self.assertGreater(active["alpha"], 0.0)
        rec_ids = {x["book_id"] for x in active["recommendations"]}
        self.assertNotIn(5, rec_ids)
        self.assertNotIn(6, rec_ids)
        self.assertEqual(active["recommendations"][0]["book_id"], 7)  # "The Locked Room" (Mystery)

    def test_active_hybrid_recommends_collaborative_and_content_winner(self):
        res = self.rec.recommend_for_home(1, top_n=5)
        self.assertEqual(res["mode"], "hybrid")
        rec_ids = [x["book_id"] for x in res["recommendations"]]
        # No debe incluir libros que ya están en la lista del usuario 1 ({1, 2, 5})
        self.assertTrue(set(rec_ids).isdisjoint({1, 2, 5}))
        # "Mars Colony" (id=3) es Sci-Fi y recomendado con 5★ por ambos vecinos MSD (usuarios 2 y 3)
        self.assertEqual(rec_ids[0], 3)
        self.assertTrue(res["recommendations"][0]["recommendation"]["reason"])

    def test_recommend_by_genre_rf13(self):
        res = self.rec.recommend_by_genre(1, "Romance", top_n=5)
        self.assertEqual(res["genre"], "Romance")
        self.assertTrue(all("Romance" in x["genres"] for x in res["recommendations"]))
        self.assertEqual(len(res["recommendations"]), 2)

    def test_item_item_similar_books_and_long_tail_fallback_rf14(self):
        # Libro 1 tiene 3 co-lectores con libro 2 (usuarios 1,2,3) y 2 co-lectores con libro 3 (usuarios 2,3)
        res = self.rec.recommend_similar_books(1, top_n=4, min_overlap=2)
        self.assertEqual(res["target_book"]["book_id"], 1)
        sim_ids = [x["book_id"] for x in res["similar_books"]]
        self.assertNotIn(1, sim_ids)
        self.assertIn(2, sim_ids[:2])
        self.assertIn(3, sim_ids[:3])

        # Libro 4 ("Quantum Stars") tiene 0 interacciones -> debe hacer fallback limpio a contenido TF-IDF (Sci-Fi)
        lt = self.rec.recommend_similar_books(4, top_n=3, min_overlap=2)
        self.assertEqual(lt["collaborative_candidates"], 0)
        self.assertEqual(lt["similar_books"][0]["similarity"]["mode"], "content_only")
        self.assertEqual(lt["similar_books"][0]["primary_genre"], "Science Fiction")

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            self.rec.recommend_for_home(99999)
        with self.assertRaises(ValueError):
            self.rec.recommend_by_genre(1, "GeneroInventado")
        with self.assertRaises(ValueError):
            self.rec.recommend_similar_books(99999)
        with self.assertRaises(ValueError):
            self.rec.recommend_for_home(1, top_n=0)
        with self.assertRaises(ValueError):
            self.rec.recommend_for_home(1, top_n=101)

    def test_extended_unrated_books_do_not_affect_recommender(self):
        # Capturar línea base exacta antes de insertar libros ampliados (in_recommender=0)
        base_n = self.rec.n_items
        base_home = [x["book_id"] for x in self.rec.recommend_for_home(1, top_n=5)["recommendations"]]
        base_genre = [x["book_id"] for x in self.rec.recommend_by_genre(1, "Science Fiction", top_n=4)["recommendations"]]

        with db_session(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO books (title,author,genres,primary_genre,synopsis,publish_year,"
                "avg_rating,ratings_count,catalog_source,in_recommender,external_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("Unrated 2025 Space Novel", "Future Author", "Science Fiction", "Science Fiction",
                 "Exploration aboard a starship across distant alien galaxies in the year 2025.",
                 2025, None, 0, "openlibrary_curated", 0, "/works/OL2025001W"),
            )
            ext_id = int(cur.lastrowid)
            # El usuario 1 agrega el libro ampliado 2025 a su lista de lectura
            conn.execute(
                "INSERT INTO reading_list (user_id, book_id, status, rating) VALUES (1, ?, 'por_leer', 5.0)",
                (ext_id,),
            )

        rec_after = HybridBookRecommender(self.db_path)
        self.assertEqual(rec_after.n_items, base_n)
        after_home = [x["book_id"] for x in rec_after.recommend_for_home(1, top_n=5)["recommendations"]]
        after_genre = [x["book_id"] for x in rec_after.recommend_by_genre(1, "Science Fiction", top_n=4)["recommendations"]]
        self.assertEqual(after_home, base_home)
        self.assertEqual(after_genre, base_genre)
        self.assertNotIn(ext_id, after_home)

        # El modal Ítem-Ítem sobre un libro in_recommender=0 proyecta TF-IDF sobre el núcleo sin fallar
        sim = rec_after.recommend_similar_books(ext_id, top_n=3)
        self.assertEqual(sim["target_book"]["book_id"], ext_id)
        self.assertEqual(sim["collaborative_candidates"], 0)
        self.assertEqual(len(sim["similar_books"]), 3)
        self.assertEqual(sim["similar_books"][0]["primary_genre"], "Science Fiction")

    def test_promoted_unrated_core_book_has_neutral_pop_score(self):
        with db_session(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO books (title,author,genres,primary_genre,synopsis,publish_year,"
                "avg_rating,ratings_count,catalog_source,in_recommender,external_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("Viaje Estelar Sin Votos", "Autor Hispano", "Science Fiction", "Science Fiction",
                 "Una novela de ciencia ficción en español sobre viajes estelares sin votos previos.",
                 2019, None, 0, "openlibrary_core", 1, "/works/OL999ESW"),
            )
            es_id = int(cur.lastrowid)
        rec = HybridBookRecommender(self.db_path)
        es_idx = rec._item_idx_of_id[es_id]
        self.assertAlmostEqual(float(rec.pop_score[es_idx]), 0.15, places=4)
        self.assertEqual(float(rec.weighted_ratings[es_idx]), 0.0)


@unittest.skipUnless(DB_PATH.exists(), "Base real no encontrada: ejecute 'python -m app.etl_seed --reset'")
class RealRecommenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rec = HybridBookRecommender()
        with db_session() as conn:
            cls.seed_users = [
                int(r["user_id"])
                for r in conn.execute(
                    "SELECT user_id FROM users WHERE password_hash IS NOT NULL AND is_seed_user = 1 ORDER BY user_id"
                ).fetchall()
            ]
            cls.top_books = [
                int(r["book_id"])
                for r in conn.execute(
                    "SELECT book_id FROM reading_list GROUP BY book_id ORDER BY COUNT(*) DESC LIMIT 3"
                ).fetchall()
            ]

    def test_three_active_seed_accounts_get_distinct_personalized_lists(self):
        self.assertEqual(len(self.seed_users), 3)
        top_sets = {}
        for uid in self.seed_users:
            res = self.rec.recommend_for_home(uid, top_n=10)
            self.assertEqual(res["mode"], "hybrid")
            self.assertEqual(len(res["recommendations"]), 10)
            self.assertGreater(res["active_neighbors"], 0)

            owned = set(self.rec.get_user_effective_ratings(uid).keys())
            rec_ids = [x["book_id"] for x in res["recommendations"]]
            self.assertTrue(owned.isdisjoint(rec_ids), f"Usuario {uid} recibió un libro ya existente en su lista")
            for x in res["recommendations"]:
                self.assertTrue(0 <= x["recommendation"]["match_percent"] <= 100)
                self.assertTrue(len(x["recommendation"]["reason"]) > 5)
            top_sets[uid] = set(rec_ids)

        # Los 3 perfiles disjuntos deben recibir listas personalizadas distintas (no idénticas)
        u1, u2, u3 = self.seed_users
        self.assertLess(len(top_sets[u1] & top_sets[u2]), 7)
        self.assertLess(len(top_sets[u1] & top_sets[u3]), 7)
        self.assertLess(len(top_sets[u2] & top_sets[u3]), 7)
        res_75 = self.rec.recommend_for_home(u1, top_n=75)
        self.assertEqual(len(res_75["recommendations"]), 75)

    def test_genre_recommendations_on_real_catalog(self):
        uid = self.seed_users[0]
        for genre in ("Science Fiction", "Mystery", "Fantasy"):
            res = self.rec.recommend_by_genre(uid, genre, top_n=8)
            self.assertEqual(res["genre"], genre)
            self.assertEqual(len(res["recommendations"]), 8)
            for x in res["recommendations"]:
                self.assertIn(genre, x["genres"])

    def test_item_item_on_top_community_books(self):
        # Los libros más co-leídos de la comunidad deben tener vecinos colaborativos con overlap >= 5
        for bid in self.top_books[:2]:
            res = self.rec.recommend_similar_books(bid, top_n=6, min_overlap=5)
            self.assertEqual(res["min_overlap_used"], 5)
            self.assertGreater(res["collaborative_candidates"], 5)
            self.assertEqual(len(res["similar_books"]), 6)
            for x in res["similar_books"]:
                self.assertGreaterEqual(x["similarity"]["overlap_users"], 5)
                self.assertIsNotNone(x["similarity"]["sim_msd_norm"])
                self.assertIsNotNone(x["similarity"]["jmsd"])

    def test_latency_under_rnf04(self):
        t0 = time.perf_counter()
        self.rec.recommend_for_home(self.seed_users[0], top_n=12)
        self.rec.recommend_by_genre(self.seed_users[1], "Fantasy", top_n=10)
        self.rec.recommend_similar_books(self.top_books[0], top_n=8)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertLess(elapsed_ms, 1500.0, f"3 llamadas tardaron {elapsed_ms:.0f} ms (RNF-04 exige < 1500 ms)")


if __name__ == "__main__":
    unittest.main()
