import React, { useEffect, useState } from 'react';
import {
  Search,
  Sparkles,
  Snowflake,
  SlidersHorizontal,
  Loader2,
  X,
  RefreshCw,
} from 'lucide-react';
import { api } from '../api/client';
import BookCard from '../components/BookCard';

const HOME_LIMIT_OPTIONS = [15, 30, 45, 60, 75];

export default function HomePage({
  user,
  genres,
  readingMap,
  listVersion,
  onSelectBook,
  onUpsertList,
  onRemoveList,
}) {
  // Estado del Buscador IR
  const [query, setQuery] = useState('');
  const [selectedGenre, setSelectedGenre] = useState('');
  const [minRating, setMinRating] = useState('');
  const [sortBy, setSortBy] = useState('relevance');
  const [exactMode, setExactMode] = useState(false);
  const [searchResult, setSearchResult] = useState(null);
  const [searching, setSearching] = useState(false);
  const [offset, setOffset] = useState(0);

  // Estado de Recomendados para ti
  const [recLimit, setRecLimit] = useState(15);
  const [homeRecs, setHomeRecs] = useState(null);
  const [loadingRecs, setLoadingRecs] = useState(true);

  const hasActiveSearch =
    query.trim().length > 0 || selectedGenre !== '' || minRating !== '';

  // Cargar recomendaciones personalizadas cada vez que cambia la lista del usuario o el límite
  useEffect(() => {
    let cancelled = false;
    setLoadingRecs(true);
    api
      .getHomeRecommendations(recLimit)
      .then((data) => {
        if (!cancelled) setHomeRecs(data);
      })
      .catch((err) => console.error('Error cargando recomendaciones:', err))
      .finally(() => {
        if (!cancelled) setLoadingRecs(false);
      });
    return () => {
      cancelled = true;
    };
  }, [user?.user_id, listVersion, recLimit]);

  // Ejecutar búsqueda IR cuando cambian los parámetros
  const runSearch = async (nextOffset = 0) => {
    setSearching(true);
    try {
      const data = await api.searchBooks({
        q: query.trim(),
        genre: selectedGenre || undefined,
        min_rating: minRating || undefined,
        sort_by: sortBy,
        exact: exactMode,
        limit: 12,
        offset: nextOffset,
      });
      setSearchResult(data);
      setOffset(nextOffset);
    } catch (err) {
      console.error('Error en búsqueda:', err);
    } finally {
      setSearching(false);
    }
  };

  useEffect(() => {
    if (!hasActiveSearch) {
      setSearchResult(null);
      setOffset(0);
      return;
    }
    const timer = setTimeout(() => {
      runSearch(0);
    }, 240);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, selectedGenre, minRating, sortBy, exactMode]);

  const clearSearch = () => {
    setQuery('');
    setSelectedGenre('');
    setMinRating('');
    setSortBy('relevance');
    setExactMode(false);
    setSearchResult(null);
    setOffset(0);
  };

  return (
    <div className="space-y-10">
      {/* Bloque 1: Buscador de Recuperación de Información (RF-04, RF-05, RF-06) */}
      <section className="rounded-3xl border border-stone-200/90 bg-white p-5 sm:p-7 shadow-sm">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="font-editorial text-2xl sm:text-3xl font-bold text-stone-900">
              Encuentra tu próxima lectura
            </h1>
          </div>
          {hasActiveSearch && (
            <button
              onClick={clearSearch}
              className="inline-flex items-center gap-1 rounded-lg bg-stone-100 px-3 py-1.5 text-xs font-semibold text-stone-600 hover:bg-stone-200 transition-colors"
            >
              <X className="h-3.5 w-3.5" />
              Limpiar filtros
            </button>
          )}
        </div>

        {/* Barra de búsqueda */}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            runSearch(0);
          }}
          className="mt-4 flex flex-col gap-3 sm:flex-row"
        >
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-stone-400" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Busca por título o autor (ej. The Great Gatsby, Dune, Atomic Habits, Onyx Storm, Agatha Christie...)"
              className="w-full rounded-xl border border-stone-300 bg-stone-50/50 py-3 pl-10 pr-4 text-sm text-stone-900 placeholder:text-stone-400 focus:border-brand-500 focus:bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20"
            />
          </div>
          <button
            type="submit"
            className="inline-flex items-center justify-center gap-2 rounded-xl bg-brand-600 px-6 py-3 text-sm font-semibold text-white shadow-sm hover:bg-brand-700 transition-colors"
          >
            {searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
            Buscar
          </button>
        </form>

        {/* Controles de filtros y ordenamiento */}
        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-stone-100 pt-4 text-xs">
          <span className="inline-flex items-center gap-1 font-semibold text-stone-500">
            <SlidersHorizontal className="h-3.5 w-3.5" />
            Filtros:
          </span>

          <select
            value={selectedGenre}
            onChange={(e) => setSelectedGenre(e.target.value)}
            className="rounded-lg border border-stone-200 bg-stone-50 px-3 py-1.5 font-medium text-stone-700 focus:border-brand-500 focus:outline-none"
          >
            <option value="">Todos los géneros</option>
            {genres.map((g) => (
              <option key={g.genre} value={g.genre}>
                {g.genre} ({g.count})
              </option>
            ))}
          </select>

          <select
            value={minRating}
            onChange={(e) => setMinRating(e.target.value)}
            className="rounded-lg border border-stone-200 bg-stone-50 px-3 py-1.5 font-medium text-stone-700 focus:border-brand-500 focus:outline-none"
          >
            <option value="">Cualquier calificación</option>
            <option value="3.5">Calificación ≥ 3.5★</option>
            <option value="4.0">Calificación ≥ 4.0★</option>
            <option value="4.3">Calificación ≥ 4.3★</option>
          </select>

          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="rounded-lg border border-stone-200 bg-stone-50 px-3 py-1.5 font-medium text-stone-700 focus:border-brand-500 focus:outline-none"
          >
            <option value="relevance">Orden: Relevancia</option>
            <option value="popularity">Orden: Popularidad</option>
            <option value="rating">Orden: Mejor calificados</option>
          </select>

          <label className="ml-auto inline-flex cursor-pointer items-center gap-2 rounded-lg border border-stone-200 bg-stone-50 px-3 py-1.5 font-medium text-stone-700 hover:bg-stone-100">
            <input
              type="checkbox"
              checked={exactMode}
              onChange={(e) => setExactMode(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-stone-300 text-brand-600 focus:ring-brand-500"
            />
            Búsqueda exacta
          </label>
        </div>
      </section>

      {/* Resultados del Buscador (si hay búsqueda o filtro activo) */}
      {hasActiveSearch && (
        <section className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="font-editorial text-2xl font-bold text-stone-900">
                Resultados de búsqueda
              </h2>
              {searchResult && (
                <p className="text-xs text-stone-500">
                  Se encontraron <strong>{searchResult.total.toLocaleString()}</strong> libros
                  {query.trim() ? ` para «${query.trim()}»` : ''}
                  {selectedGenre ? ` en ${selectedGenre}` : ''}.
                </p>
              )}
            </div>
          </div>

          {searching && !searchResult ? (
            <div className="flex items-center justify-center py-12 text-stone-500">
              <Loader2 className="h-6 w-6 animate-spin text-brand-600" />
            </div>
          ) : searchResult?.results?.length > 0 ? (
            <>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {searchResult.results.map((book) => (
                  <BookCard
                    key={book.book_id}
                    book={book}
                    userEntry={readingMap[book.book_id]}
                    extraMeta={
                      book.relevance != null && (
                        <span className="inline-block rounded-md bg-stone-100 px-2 py-0.5 text-[11px] font-medium text-stone-600">
                          Relevancia IR: {Math.round(book.relevance * 100)}%
                        </span>
                      )
                    }
                    onSelectBook={onSelectBook}
                    onUpsertList={onUpsertList}
                    onRemoveList={onRemoveList}
                  />
                ))}
              </div>

              {/* Paginación */}
              {searchResult.total > 12 && (
                <div className="flex items-center justify-center gap-3 pt-2">
                  <button
                    disabled={offset === 0 || searching}
                    onClick={() => runSearch(Math.max(0, offset - 12))}
                    className="rounded-xl border border-stone-200 bg-white px-4 py-2 text-xs font-semibold text-stone-700 hover:bg-stone-50 disabled:opacity-40"
                  >
                    Anterior
                  </button>
                  <span className="text-xs font-medium text-stone-500">
                    Mostrando {offset + 1}–{Math.min(offset + 12, searchResult.total)} de{' '}
                    {searchResult.total}
                  </span>
                  <button
                    disabled={offset + 12 >= searchResult.total || searching}
                    onClick={() => runSearch(offset + 12)}
                    className="rounded-xl border border-stone-200 bg-white px-4 py-2 text-xs font-semibold text-stone-700 hover:bg-stone-50 disabled:opacity-40"
                  >
                    Siguiente
                  </button>
                </div>
              )}
            </>
          ) : (
            <div className="rounded-2xl border border-dashed border-stone-300 bg-white/70 p-10 text-center">
              <p className="text-sm font-semibold text-stone-700">
                No encontramos libros con esos criterios.
              </p>
              <p className="mt-1 text-xs text-stone-500">
                Prueba desactivando «Búsqueda exacta» o ampliando los filtros de género/calificación.
              </p>
            </div>
          )}
        </section>
      )}

      {/* Bloque 2: Ubicación 1 del Recomendador — "Recomendados para ti" (RF-11, RF-12) */}
      <section className="space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2.5">
              <h2 className="font-editorial text-2xl sm:text-3xl font-bold text-stone-900">
                Recomendados para ti
              </h2>
              {homeRecs && (
                <span
                  className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold ${homeRecs.mode === 'cold_start'
                    ? 'bg-sky-50 text-sky-800 ring-1 ring-sky-200'
                    : 'bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200'
                    }`}
                >
                  {homeRecs.mode === 'cold_start' ? (
                    <>
                      <Snowflake className="h-3.5 w-3.5" />
                      Géneros favoritos + Popularidad
                    </>
                  ) : (
                    <>
                      <Sparkles className="h-3.5 w-3.5" />
                      Recomendador Híbrido Activo
                    </>
                  )}
                </span>
              )}
            </div>
            <p className="mt-1 text-xs sm:text-sm text-stone-500">
              {homeRecs?.mode === 'cold_start'
                ? 'Agrega libros a tu lista de lectura o califícalos para activar el recomendador en tiempo real.'
                : 'Personalizado dinámicamente con tu lista de lectura, tus calificaciones y lectores afines de la comunidad.'}
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2.5">
            <div className="flex items-center gap-2">
              <label
                htmlFor="home-rec-limit"
                className="text-xs font-semibold text-stone-500"
              >
                Mostrar:
              </label>
              <select
                id="home-rec-limit"
                disabled={loadingRecs}
                value={recLimit}
                onChange={(e) => setRecLimit(Number(e.target.value))}
                className="rounded-xl border border-stone-200 bg-white px-3 py-2 text-xs font-semibold text-stone-700 focus:border-brand-500 focus:outline-none disabled:opacity-60"
              >
                {HOME_LIMIT_OPTIONS.map((n) => (
                  <option key={n} value={n}>
                    {n} libros
                  </option>
                ))}
              </select>
            </div>

            <button
              disabled={loadingRecs}
              onClick={() => {
                setLoadingRecs(true);
                api
                  .getHomeRecommendations(recLimit)
                  .then(setHomeRecs)
                  .catch((err) => console.error('Error cargando recomendaciones:', err))
                  .finally(() => setLoadingRecs(false));
              }}
              className="inline-flex items-center gap-1.5 rounded-xl border border-stone-200 bg-white px-3.5 py-2 text-xs font-semibold text-stone-700 hover:bg-stone-50 disabled:opacity-60 transition-colors"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loadingRecs ? 'animate-spin' : ''}`} />
              Recalcular
            </button>
          </div>
        </div>

        {loadingRecs && !homeRecs ? (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {[1, 2, 3, 4, 5, 6].map((n) => (
              <div
                key={n}
                className="h-64 animate-pulse rounded-2xl border border-stone-200 bg-white/80 p-5"
              />
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {(homeRecs?.recommendations || []).map((book) => (
              <BookCard
                key={book.book_id}
                book={book}
                userEntry={readingMap[book.book_id]}
                recommendation={book.recommendation}
                onSelectBook={onSelectBook}
                onUpsertList={onUpsertList}
                onRemoveList={onRemoveList}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
