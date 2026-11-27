# BookSphere — Buscador IR y Sistema de Recomendación Híbrido de Libros

Aplicación web comunitaria para el descubrimiento literario que integra un **buscador de Recuperación de Información (BM25 + TF-IDF de n-gramas)** con un **Sistema de Recomendación Híbrido** basado en filtrado colaborativo en memoria (**MSD / JMSD / Chebyshev**), factorización de matrices (**TruncatedSVD**), filtrado basado en contenido (**TF-IDF + Similitud del Coseno**) y popularidad bayesiana.

> **Asignatura:** Web Social — Departamento de Telemática, Universidad del Cauca.
 **Docente:** Cristhian Figueroa Martinez
 **Estudiante:** Nicolás Sánchez Perea

---

## 1. Arquitectura del Sistema

El sistema sigue una arquitectura **Full-Stack desacoplada**:

```text
Frontend (React + Vite + TailwindCSS :5173)
   │  Proxy REST (/api -> http://127.0.0.1:8000)
   ▼
Backend API (FastAPI + Uvicorn :8000)
   ├── search_engine.py  ──► Índice BM25 ponderado + n-gramas (15,000 libros locales)
   ├── recommender.py    ──► Recomendador Híbrido en 3 Niveles (aislado en in_recommender=1):
   │                          • Nivel 1 (Contenido): TF-IDF + Similitud del Coseno
   │                          • Nivel 2 (Colaborativo): MSD / JMSD / Chebyshev + TruncatedSVD (k=30)
   │                          • Nivel 3 (Hibridación): Cold Start (géneros + pop.) / Híbrido adaptativo
   ├── community.py      ──► Vecindario MSD (Top 10 público) y Favoritos de la Comunidad
   ├── evaluation.py     ──► Benchmark offline 80/20 estratificado por usuario (5 modelos)
   ├── etl_seed.py       ──► Escaneo completo Amazon Books 2023 + enriquecimiento
   ├── enrich_catalog.py ──► Ampliación offline Open Library (in_recommender=0)
   └── database.py       ──► Persistencia local en SQLite (booksphere_community.db)
```

### Arquitectura de Catálogo en Dos Niveles (`in_recommender`)
* **Nivel 1 — Núcleo Recomendador (`in_recommender = 1`, 11,549 libros):**
  * **10,842 obras únicas** deduplicadas de Amazon Reviews 2023 (`catalog_source = 'amazon_2023'`) con calidad mínima **`avg_rating >= 3.8★`**, exclusión de formatos no-libro (`CD`, `Box Set`, `Coloring Book`, `Workbook`, `Flash Cards`, `Sound Book`, paquetes multi-título) y estandarización de autores/iniciales (`C. S. Lewis`, `George R. R. Martin`, `R. F. Kuang`, `J. K. Rowling`) (`450` usuarios, `35,388` interacciones reales sobre `3,136` obras; `8,413` libros en cola larga cubiertos por TF-IDF).
  * **+707 libros curados promovidos al núcleo:** `628` obras vía Open Library (`catalog_source = 'openlibrary_core'`, incluyendo literatura originalmente en **español** de autores hispanohablantes, clásicos y autores contemporáneos), `5` títulos adicionales (`catalog_source = 'openlibrary_curated'`) y `74` títulos de sagas curadas manualmente (`catalog_source = 'manual_curated'`).
* **Nivel 2 — Catálogo Ampliado (`in_recommender = 0`, 3,451 libros):** compuesto por **`2,977`** lanzamientos recientes **`2024–2025`** (`catalog_source = 'openlibrary_curated'`), **`445`** obras complementarias de Open Library (`catalog_source = 'openlibrary_core'`) y **`29`** volúmenes de sagas curadas manualmente (`catalog_source = 'manual_curated'`), alcanzando **`3,028` títulos publicados desde 2024 (`1508–2026`)**. Son 100 % buscables (incluidas *stopwords* bilingües EN/ES) y agregables a `reading_list` sin alterar el recomendador ni el benchmark 80/20.

### Reglas de Negocio Clave
* **Votos implícitos temporales:** Si un libro en `reading_list` no tiene calificación explícita (`1–5★`), el recomendador asigna un voto implícito temporal según su estado: `por_leer = 3.0★`, `leyendo = 3.7★`, `leido = 4.0★`. Al calificar explícitamente, la nota de usuario prevalece.
* **Hibridación explícita (Switching + Weighted):**
  * *Cold Start* (`0` libros en lista): `0.65 * CBF(géneros_favoritos) + 0.35 * PopularidadBayesiana`.
  * *Usuario Activo* (`≥ 1` libro en lista): `α * Score_CF + (1 - α) * Score_CBF`, con `α = min(0.65, 0.20 + 0.05 * |I_u|)` y `Score_CF = 0.60 * MSD + 0.40 * SVD`.
* **Manejo estricto de dispersión:** Las similitudes MSD, JMSD y Chebyshev se calculan únicamente sobre la intersección co-evaluada (nunca se imputan ceros en `NaN`) con umbral mínimo de solapamiento `γ ≥ 2` (`γ = 3` en usuarios, `γ = 5` en ítems).
* **Tres puntos de actuación del recomendador en la interfaz:**
  1. **Inicio:** *"Recomendados para ti"*.
  2. **Pestaña de Géneros:** *"Porque te interesa el género [X]"*.
  3. **Detalle del Libro:** *"Lectores similares también agregaron..."* (Ítem-Ítem MSD/JMSD + Contenido, o fallback TF-IDF de solo lectura para libros ampliados `in_recommender = 0`).

---

## 2. Estructura del Repositorio

```text
proyecto_final_recomendador/
├── .gitignore
├── README.md
├── backend/
│   ├── requirements.txt
│   ├── data/
│   │   ├── booksphere_community.db     # Base SQLite curada (15,000 libros 1508–2026, 450 usuarios, 35,388 ratings)
│   │   ├── .gitkeep
│   │   └── tests/
│   │       ├── test_search_engine.py   # Pruebas del buscador IR y calidad del catálogo
│   │       ├── test_recommender.py     # Pruebas del recomendador híbrido, invarianza y reglas implícitas
│   │       └── test_api_and_community.py # Pruebas API FastAPI, comunidad Top 10 y evaluación
│   └── app/
│       ├── __init__.py
│       ├── main.py                     # Servidor FastAPI y endpoints REST (/docs)
│       ├── database.py                 # Esquema SQLite (in_recommender, catalog_source) y PBKDF2-SHA256
│       ├── etl_seed.py                 # Streaming completo Amazon Reviews 2023 + paso offline [4/4]
│       ├── enrich_catalog.py           # Enriquecimiento offline vía Open Library
│       ├── openlibrary_client.py       # Sanitización estricta de texto/puntuación y cliente Open Library
│       ├── search_engine.py            # Buscador IR (BM25 + char n-grams + filtros) sobre los 15,000 libros
│       ├── recommender.py              # Recomendador híbrido de 3 niveles (aislado en in_recommender=1)
│       ├── community.py                # Vecindario de lectores (Top 10) y favoritos
│       └── evaluation.py               # Evaluación offline 80/20 (MAE, RMSE, P@K, Recall@K, NDCG@K)
└── frontend/
    ├── package.json
    ├── vite.config.js                  # Proxy /api -> 127.0.0.1:8000
    ├── tailwind.config.js
    └── src/
        ├── App.jsx
        ├── api/client.js
        ├── components/                 # Navbar, BookCard, BookModal
        └── pages/                      # AuthPage, HomePage, GenresPage, MyListPage, CommunityPage, MetricsPage
```

### Archivos Protegidos en `.gitignore`
El archivo [`.gitignore`](.gitignore) protege explícitamente los archivos sensibles y temporales para que **nunca se suban al repositorio**:
* `backend/data/seed_credentials.private.txt` *(credenciales privadas de las 3 cuentas semilla de prueba).*
* `__pycache__/`, `*.pyc`, `.venv/`, `node_modules/`, `dist/`.

---

## 3. Requisitos e Instalación

* **Python:** `3.11+` (probado en Python `3.13`)
* **Node.js:** `18+` (probado en Node `24` / npm `11`)

### Paso 1 — Instalar dependencias del Backend
```powershell
cd backend
python -m pip install -r requirements.txt
```

### Paso 2 — Instalar dependencias del Frontend
```powershell
cd frontend
npm install
```

### Paso 3 — Base de datos (Opcional: reconstruir 100 % desde fuentes remotas)
La base de datos curada `backend/data/booksphere_community.db` (~22.4 MB) ya viene lista con **15,000 libros únicos** (`0 BLOB`, `0 ::`, iniciales de autor estandarizadas, español original en autores hispanos). Si deseas regenerarla desde cero mediante streaming HTTPS completo de **Amazon Reviews 2023** seguido del enriquecimiento offline vía **Open Library**:

```powershell
cd backend
python -m app.etl_seed --reset
```
* Opciones útiles del CLI:
  * `python -m app.etl_seed --reset --skip-enrich` *(construye únicamente el núcleo Amazon de 12,000 libros).*
  * `python -m app.enrich_catalog --core-target 12000 --extended-target 3000 --reset-extended` *(re-ejecuta el enriquecimiento en dos niveles sin re-descargar Amazon).*
  * `python -m app.enrich_catalog --dedupe-db --core-target 12000 --extended-target 3000` *(estandariza autores/títulos, deduplica in-place y rellena cupos abiertos vía Open Library).*

> El pipeline genera `booksphere_community.db` limpia (`0 BLOB`, `0 ::`, `0` mojibake, `0` texto no latino) y guarda las contraseñas aleatorias de las 3 cuentas semilla disjuntas únicamente en `backend/data/seed_credentials.private.txt` (ignorado por `.gitignore`).

---

## 4. Ejecución de la Aplicación (Desarrollo Local)

Abre **dos terminales**:

### Terminal 1 — Backend (FastAPI en puerto `8000`)
```powershell
cd backend
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```
* **Documentación interactiva Swagger UI:** `http://127.0.0.1:8000/docs`

### Terminal 2 — Frontend (React + Vite en puerto `5173`)
```powershell
cd frontend
npm run dev
```
* **Aplicación Web:** `http://127.0.0.1:5173/`

Para verificar compilación de producción del frontend:
```powershell
cd frontend
npm run build
```

---

## 5. Ejecución de Pruebas Automatizadas

La suite incluye pruebas unitarias y de integración sobre bases sintéticas aisladas y sobre la base real `booksphere_community.db`:

```powershell
cd backend
python -m unittest discover -s data/tests -t . -v
```

---

## 6. Ejecución de la Evaluación Cuantitativa (`evaluation.py`)

El módulo `app/evaluation.py` ejecuta un protocolo **80 % Entrenamiento / 20 % Prueba** estratificado por usuario (semilla `42`, sin fuga de datos) comparando 5 modelos: *Baseline*, *Chebyshev ($L_\infty$)*, *MSD / JMSD*, *TruncatedSVD* y el *Recomendador Híbrido Propuesto*.

### Opción A — Ejecutar directamente desde Python (CLI)
```powershell
cd backend
python -c "import json; from app.recommender import HybridBookRecommender; from app.evaluation import run_offline_evaluation; print(json.dumps(run_offline_evaluation(HybridBookRecommender()), indent=2, ensure_ascii=False))"
```

### Opción B — Consultar vía Endpoint REST (con el backend encendido)
* Abrir en navegador o `curl`: `http://127.0.0.1:8000/api/evaluation`
* Para forzar recálculo: `http://127.0.0.1:8000/api/evaluation?recompute=true`

### Resultados Referenciales del Benchmark (Catálogo curado 15,000 libros · 120 usuarios · 33,541 train · 1,832 test · 2,562 candidatos)

| Modelo | MAE ↓ | RMSE ↓ | Cob. Pred. | Precision@5 ↑ | Precision@10 ↑ | Recall@10 ↑ | NDCG@10 ↑ | Cob. Catálogo |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Baseline (Media Usuario-Ítem) | 0.6340 | 0.8354 | 100.0 % | 0.0150 | 0.0125 | 0.0112 | 0.0128 | 5.74 % |
| CF — Distancia de Chebyshev ($L_\infty$) | 0.6820 | 0.9102 | 77.07 % | 0.0450 | 0.0483 | 0.0400 | 0.0490 | 10.50 % |
| CF — MSD / JMSD | 0.6572 | 0.8757 | 86.19 % | 0.1083 | 0.1017 | 0.0825 | 0.1117 | 14.48 % |
| ML — Factorización TruncatedSVD | 0.6639 | 0.8607 | 100.0 % | 0.0167 | 0.0167 | 0.0121 | 0.0173 | 20.92 % |
| **Recomendador Híbrido Propuesto** | **0.6428** | **0.8473** | **100.0 %** | **0.1683** | **0.1392** | **0.1146** | **0.1466** | **16.20 %** |
