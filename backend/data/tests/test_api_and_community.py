"""Pruebas integrales de Fase 4: Comunidad (Top 10 y Favoritos), Evaluación Offline y API REST FastAPI.

Ejecutar desde ``backend/``:
    python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.community import (
    PUBLIC_LIST_MAX_BOOKS,
    get_community_favorites,
    get_user_neighborhood,
    get_user_public_top_books,
)
from app.database import DATA_DIR, DB_PATH, db_session, hash_password, init_db
from app.evaluation import run_offline_evaluation
from app.main import create_app
from app.recommender import HybridBookRecommender


def build_api_test_db(path: Path) -> None:
    """BD sintética con 15 libros para probar el tope estricto Top 10 en listas públicas de comunidad."""
    init_db(path)
    books = [
        (f"SciFi Book {i}", f"Author SciFi {i}", "Science Fiction, Fiction", "Science Fiction",
         f"A thrilling space odyssey across distant galaxies and futuristic worlds volume {i}.", 2000 + i, 4.4, 10000 + i * 500)
        for i in range(1, 9)
    ] + [
        (f"Mystery Book {i}", f"Author Mystery {i}", "Mystery, Thriller", "Mystery",
         f"A gripping detective murder mystery filled with clues and suspense chapter {i}.", 1995 + i, 4.3, 8000 + i * 400)
        for i in range(1, 8)
    ]
    with db_session(path) as conn:
        conn.executemany(
            "INSERT INTO books (title,author,genres,primary_genre,synopsis,publish_year,avg_rating,ratings_count)"
            " VALUES (?,?,?,?,?,?,?,?)",
            books,
        )
        # Usuario 1 (activo con login) y Usuario 2 (vecino con 14 libros > 10 para probar tope Top 10)
        conn.executemany(
            "INSERT INTO users (user_id,username,password_hash,display_name,favorite_genres,is_seed_user)"
            " VALUES (?,?,?,?,?,?)",
            [
                (1, "active_seed", hash_password("secret_pass"), "Active Seed", "Science Fiction, Mystery", 1),
                (2, "prolific_neighbor", None, "Prolific Neighbor", "Science Fiction, Mystery", 1),
                (3, "scifi_reader", None, "SciFi Reader", "Science Fiction", 1),
            ],
        )
        interactions = []
        # Usuario 1 lee 6 libros
        for b_id, rat in [(1, 5.0), (2, 5.0), (3, 4.0), (4, 4.0), (9, 3.0), (10, 4.0)]:
            interactions.append((1, b_id, "leido", rat))
        # Usuario 2 lee 14 de los 15 libros (debe limitarse a Top 10 al consultar su lista pública!)
        for b_id in range(1, 15):
            rat = 5.0 if b_id <= 5 else (4.0 if b_id <= 10 else 3.0)
            interactions.append((2, b_id, "leido", rat))
        # Usuario 3 lee 5 libros Sci-Fi
        for b_id in range(1, 6):
            interactions.append((3, b_id, "leido", 5.0))
        conn.executemany(
            "INSERT INTO reading_list (user_id,book_id,status,rating) VALUES (?,?,?,?)",
            interactions,
        )


class FastAPIIsolatedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "api_test.db"
        build_api_test_db(self.db_path)
        self.app = create_app(self.db_path)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self._tmp.cleanup()

    def test_full_auth_cold_start_dynamic_list_and_logoff_flow(self):
        # 1. Health check
        h = self.client.get("/api/health")
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.json()["books_indexed"], 15)
        self.assertEqual(h.json()["public_list_max_books"], 10)

        # 2. Registro desde cero (RF-01: Cold Start)
        reg = self.client.post("/api/auth/register", json={
            "username": "nuevo_lector",
            "password": "mi_password_123",
            "display_name": "Nuevo Lector",
            "favorite_genres": ["Science Fiction"],
        })
        self.assertEqual(reg.status_code, 201)
        token = reg.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(reg.json()["user"]["stats"]["recommender_mode"], "cold_start")

        # Registro duplicado -> 409
        dup = self.client.post("/api/auth/register", json={
            "username": "nuevo_lector",
            "password": "otra_password",
            "favorite_genres": ["Mystery"],
        })
        self.assertEqual(dup.status_code, 409)

        # 3. Home en Cold Start (RF-11)
        home_cold = self.client.get("/api/recommendations/home?top_n=5", headers=headers)
        self.assertEqual(home_cold.status_code, 200)
        self.assertEqual(home_cold.json()["mode"], "cold_start")
        self.assertEqual(home_cold.json()["recommendations"][0]["primary_genre"], "Science Fiction")

        # 4. Buscador IR (RF-04..06)
        srch = self.client.get("/api/books/search?q=space&genre=Science+Fiction&sort_by=relevance", headers=headers)
        self.assertEqual(srch.status_code, 200)
        self.assertGreater(srch.json()["total"], 0)

        # 5. Lista de lectura dinámica + votos implícitos (RF-07..10, RNF-06)
        r1 = self.client.put("/api/reading-list/1", json={"status": "por_leer"}, headers=headers)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["effective_rating"], 3.0)
        self.assertTrue(r1.json()["is_implicit_rating"])

        r2 = self.client.put("/api/reading-list/1", json={"status": "leyendo"}, headers=headers)
        self.assertEqual(r2.json()["effective_rating"], 3.7)

        r3 = self.client.put("/api/reading-list/2", json={"status": "leido", "rating": 5}, headers=headers)
        self.assertEqual(r3.json()["effective_rating"], 5.0)
        self.assertFalse(r3.json()["is_implicit_rating"])

        # Verificar cambio inmediato a modo híbrido y exclusión de libros 1 y 2
        home_active = self.client.get("/api/recommendations/home?top_n=5", headers=headers)
        self.assertEqual(home_active.status_code, 200)
        self.assertEqual(home_active.json()["mode"], "hybrid")
        rec_ids = {x["book_id"] for x in home_active.json()["recommendations"]}
        self.assertNotIn(1, rec_ids)
        self.assertNotIn(2, rec_ids)

        # 6. Recomendación por género y detalle Ítem-Ítem (RF-13, RF-14)
        by_g = self.client.get("/api/recommendations/genre/Mystery?top_n=4", headers=headers)
        self.assertEqual(by_g.status_code, 200)
        self.assertTrue(all("Mystery" in x["genres"] for x in by_g.json()["recommendations"]))

        sim = self.client.get("/api/recommendations/similar/1?top_n=4&min_overlap=2", headers=headers)
        self.assertEqual(sim.status_code, 200)
        self.assertGreater(len(sim.json()["similar_books"]), 0)

        # 7. Comunidad: Vecindario + Top 10 estricto de usuario con 14 libros (RF-16)
        nb = self.client.get("/api/community/neighborhood", headers=headers)
        self.assertEqual(nb.status_code, 200)
        self.assertGreater(len(nb.json()["neighbors"]), 0)

        pub_top = self.client.get("/api/community/users/2/top-books", headers=headers)
        self.assertEqual(pub_top.status_code, 200)
        pub_data = pub_top.json()
        self.assertEqual(pub_data["total_books_in_list"], 14)
        self.assertEqual(pub_data["shown_count"], PUBLIC_LIST_MAX_BOOKS)
        self.assertEqual(len(pub_data["top_books"]), 10)
        # Los libros 1 y 2 deben aparecer marcados como ya en la lista del usuario autenticado
        in_list_flags = {b["book_id"]: b["in_viewer_list"] for b in pub_data["top_books"]}
        self.assertTrue(in_list_flags.get(1))
        self.assertTrue(in_list_flags.get(2))

        # 8. Favoritos de la Comunidad (RF-17)
        favs = self.client.get("/api/community/favorites?limit=5", headers=headers)
        self.assertEqual(favs.status_code, 200)
        self.assertEqual(len(favs.json()["most_read"]), 5)
        self.assertEqual(len(favs.json()["highest_rated"]), 5)

        # 9. Eliminar libro de la lista
        rem = self.client.delete("/api/reading-list/1", headers=headers)
        self.assertTrue(rem.json()["removed"])

        # 10. Logoff (RF-03) -> el token deja de servir de inmediato
        lo = self.client.post("/api/auth/logout", headers=headers)
        self.assertTrue(lo.json()["logged_out"])
        me_after = self.client.get("/api/auth/me", headers=headers)
        self.assertEqual(me_after.status_code, 401)

        # 11. Login con cuenta real existente (RF-02) + rechazo con contraseña incorrecta
        bad_login = self.client.post("/api/auth/login", json={"username": "active_seed", "password": "wrong"})
        self.assertEqual(bad_login.status_code, 401)
        ok_login = self.client.post("/api/auth/login", json={"username": "active_seed", "password": "secret_pass"})
        self.assertEqual(ok_login.status_code, 200)
        self.assertEqual(ok_login.json()["user"]["username"], "active_seed")

    def test_extended_unrated_2025_book_search_add_and_similar_flow(self):
        with db_session(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO books (title,author,genres,primary_genre,synopsis,publish_year,"
                "avg_rating,ratings_count,catalog_source,in_recommender,external_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("Beyond 2025 Nebula", "Elena Vance", "Science Fiction", "Science Fiction",
                 "A thrilling 2025 space odyssey across distant galaxies.",
                 2025, None, 0, "openlibrary_curated", 0, "/works/OL2025TESTW"),
            )
            ext_id = int(cur.lastrowid)

        app = create_app(self.db_path)
        client = TestClient(app)
        try:
            h = client.get("/api/health").json()
            self.assertEqual(h["books_indexed"], 16)
            self.assertEqual(h["recommender_core_books"], 15)

            login = client.post("/api/auth/login", json={"username": "active_seed", "password": "secret_pass"})
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            srch = client.get("/api/books/search?q=Beyond+2025+Nebula", headers=headers)
            self.assertEqual(srch.status_code, 200)
            self.assertEqual(srch.json()["results"][0]["book_id"], ext_id)
            self.assertFalse(srch.json()["results"][0]["in_recommender"])
            self.assertIsNone(srch.json()["results"][0]["avg_rating"])

            add = client.put(f"/api/reading-list/{ext_id}", json={"status": "por_leer"}, headers=headers)
            self.assertEqual(add.status_code, 200)

            rl = client.get("/api/reading-list", headers=headers).json()
            added_row = next(x for x in rl["items"] if x["book_id"] == ext_id)
            self.assertFalse(added_row["in_recommender"])

            sim = client.get(f"/api/recommendations/similar/{ext_id}?top_n=4", headers=headers)
            self.assertEqual(sim.status_code, 200)
            self.assertEqual(sim.json()["collaborative_candidates"], 0)
            self.assertGreater(len(sim.json()["similar_books"]), 0)
        finally:
            client.close()


@unittest.skipUnless(DB_PATH.exists(), "Base real no encontrada: ejecute 'python -m app.etl_seed --reset'")
class RealPhase4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rec = HybridBookRecommender()
        with db_session() as conn:
            row = conn.execute(
                "SELECT u.user_id, COUNT(rl.id) AS n "
                "FROM users u JOIN reading_list rl ON u.user_id = rl.user_id "
                "WHERE u.password_hash IS NOT NULL AND u.is_seed_user = 1 "
                "GROUP BY u.user_id ORDER BY n DESC LIMIT 1"
            ).fetchone()
            cls.top_seed_uid = int(row["user_id"])

    def test_community_neighborhood_and_top10_cap_on_real_db(self):
        # El lector semilla más activo tiene > 50 libros y múltiples vecinos MSD reales
        nb = get_user_neighborhood(self.top_seed_uid, self.rec, limit=10)
        self.assertEqual(nb["mode"], "collaborative_msd")
        self.assertEqual(len(nb["neighbors"]), 10)
        top_neighbor_id = nb["neighbors"][0]["user_id"]

        # Verificar que incluso para un lector con > 50 libros, la lista pública se recorta a Top 10
        pub = get_user_public_top_books(self.top_seed_uid, viewer_user_id=top_neighbor_id, limit=25)
        self.assertGreater(pub["total_books_in_list"], 25)
        self.assertEqual(pub["shown_count"], 10)
        self.assertEqual(len(pub["top_books"]), 10)

    def test_community_favorites_on_real_db(self):
        favs = get_community_favorites(limit=10)
        self.assertEqual(len(favs["most_read"]), 10)
        self.assertEqual(len(favs["highest_rated"]), 10)
        self.assertGreater(favs["community_stats"]["total_interactions"], 5000)
        # El orden de most_read debe ser descendente por community_readers
        readers = [b["community_readers"] for b in favs["most_read"]]
        self.assertEqual(readers, sorted(readers, reverse=True))

    def test_offline_evaluation_5_models_on_real_db(self):
        report = run_offline_evaluation(self.rec, max_eval_users=60)
        self.assertEqual(len(report["models"]), 5)
        self.assertGreater(report["protocol"]["test_ratings"], 100)
        by_id = {m["id"]: m for m in report["models"]}
        for mid in ("baseline", "chebyshev", "msd_jmsd", "svd_ml", "hybrid"):
            m = by_id[mid]
            self.assertIsNotNone(m["mae"])
            self.assertIsNotNone(m["rmse"])
            self.assertGreater(m["mae"], 0.0)
            self.assertGreater(m["rmse"], 0.0)
            self.assertGreaterEqual(m["ndcg_at_10"], 0.0)

    def test_private_seed_account_login_without_leaking_credentials(self):
        cred_path = DATA_DIR / "seed_credentials.private.txt"
        if not cred_path.exists():
            self.skipTest("Archivo de credenciales privadas no presente.")
        username = password = None
        for line in cred_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("usuario") and username is None:
                username = line.split(":", 1)[1].strip()
            elif line.startswith("contraseña") and password is None:
                password = line.split(":", 1)[1].strip()
        self.assertTrue(username and password)

        client = TestClient(create_app(DB_PATH))
        try:
            ok = client.post("/api/auth/login", json={"username": username, "password": password})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json()["user"]["username"], username)
            self.assertGreater(ok.json()["user"]["stats"]["total_books"], 20)
            bad = client.post("/api/auth/login", json={"username": username, "password": "wrong_password"})
            self.assertEqual(bad.status_code, 401)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
