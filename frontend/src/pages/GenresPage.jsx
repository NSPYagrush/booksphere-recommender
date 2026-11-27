import React, { useEffect, useState } from 'react';
import { Compass, Sparkles, Star, Loader2 } from 'lucide-react';
import { api } from '../api/client';
import BookCard from '../components/BookCard';

export default function GenresPage({
  user,
  genres,
  readingMap,
  listVersion,
  onSelectBook,
  onUpsertList,
  onRemoveList,
}) {
  const favSet = new Set(user?.favorite_genres || []);

  // Ordenar géneros poniendo los favoritos del usuario al principio
  const orderedGenres = [...genres]
    .filter((g) => !['Fiction', 'Nonfiction'].includes(g.genre))
    .sort((a, b) => {
      const fa = favSet.has(a.genre) ? 1 : 0;
      const fb = favSet.has(b.genre) ? 1 : 0;
      if (fa !== fb) return fb - fa;
      return b.count - a.count;
    });

  const [activeGenre, setActiveGenre] = useState(
    () => user?.favorite_genres?.[0] || orderedGenres[0]?.genre || 'Fantasy'
  );
  const [genreRecs, setGenreRecs] = useState(null);
  const [catalog, setCatalog] = useState(null);
  const [sortBy, setSortBy] = useState('popularity');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!activeGenre && orderedGenres.length > 0) {
      setActiveGenre(orderedGenres[0].genre);
    }
  }, [orderedGenres.length]);

  useEffect(() => {
    if (!activeGenre) return;
    let cancelled = false;
    setLoading(true);

    Promise.all([
      api.getGenreRecommendations(activeGenre, 9),
      api.searchBooks({ genre: activeGenre, sort_by: sortBy, limit: 12, offset: 0 }),
    ])
      .then(([recs, cat]) => {
        if (!cancelled) {
          setGenreRecs(recs);
          setCatalog(cat);
        }
      })
      .catch((err) => console.error('Error cargando género:', err))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [activeGenre, sortBy, listVersion]);

  return (
    <div className="space-y-8">
      {/* Cabecera y selector interactivo de géneros */}
      <section className="rounded-3xl border border-stone-200/90 bg-white p-6 shadow-sm">
        <div className="flex items-center gap-2.5">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-100 text-brand-700">
            <Compass className="h-5 w-5" />
          </div>
          <div>
            <h1 className="font-editorial text-2xl sm:text-3xl font-bold text-stone-900">
              Explorar por Géneros
            </h1>
            <p className="text-xs sm:text-sm text-stone-500">
              Selecciona cualquier categoría literaria para ver recomendaciones personalizadas y el
              catálogo completo del género.
            </p>
          </div>
        </div>

        <div className="mt-5 flex flex-wrap gap-2">
          {orderedGenres.map((g) => {
            const isSelected = activeGenre === g.genre;
            const isFav = favSet.has(g.genre);
            return (
              <button
                key={g.genre}
                onClick={() => setActiveGenre(g.genre)}
                className={`inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-semibold transition-all ${isSelected
                  ? 'bg-brand-600 text-white shadow-sm'
                  : isFav
                    ? 'bg-amber-50 text-amber-900 ring-1 ring-amber-300 hover:bg-amber-100'
                    : 'bg-stone-100 text-stone-700 hover:bg-stone-200'
                  }`}
              >
                {isFav && (
                  <Star
                    className={`h-3 w-3 ${isSelected ? 'fill-amber-300 text-amber-300' : 'fill-amber-500 text-amber-500'
                      }`}
                  />
                )}
                <span>{g.genre}</span>
                <span
                  className={`rounded-full px-1.5 py-0.5 text-[10px] ${isSelected ? 'bg-white/20 text-white' : 'bg-white/80 text-stone-500'
                    }`}
                >
                  {g.count}
                </span>
              </button>
            );
          })}
        </div>
      </section>

      {loading ? (
        <div className="flex flex-col items-center justify-center py-16 text-stone-500">
          <Loader2 className="h-7 w-7 animate-spin text-brand-600" />
          <p className="mt-2 text-sm">Calculando recomendaciones en {activeGenre}...</p>
        </div>
      ) : (
        <>
          {/* Ubicación 2 del Recomendador: Porque te interesa el género [X] */}
          <section className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="flex items-center gap-2 font-editorial text-2xl font-bold text-stone-900">
                  <Sparkles className="h-5 w-5 text-brand-600" />
                  Porque te interesa el género «{activeGenre}»
                </h2>
                <p className="text-xs text-stone-500">
                  Selección personalizada del recomendador filtrada al género{' '}
                  <strong>{activeGenre}</strong> ({genreRecs?.total_in_genre?.toLocaleString()}{' '}
                  títulos disponibles en el catálogo).
                </p>
              </div>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {(genreRecs?.recommendations || []).map((book) => (
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
          </section>

          {/* Catálogo general del género */}
          <section className="space-y-4 border-t border-stone-200 pt-8">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h3 className="font-editorial text-xl font-bold text-stone-900">
                  Destacados del catálogo en {activeGenre}
                </h3>
                <p className="text-xs text-stone-500">
                  Explora los títulos más populares o mejor valorados del género.
                </p>
              </div>
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value)}
                className="rounded-xl border border-stone-200 bg-white px-3.5 py-2 text-xs font-semibold text-stone-700 focus:border-brand-500 focus:outline-none"
              >
                <option value="popularity">Más populares</option>
                <option value="rating">Mejor calificados</option>
              </select>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {(catalog?.results || []).map((book) => (
                <BookCard
                  key={book.book_id}
                  book={book}
                  userEntry={readingMap[book.book_id]}
                  onSelectBook={onSelectBook}
                  onUpsertList={onUpsertList}
                  onRemoveList={onRemoveList}
                />
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
