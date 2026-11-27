import React, { useEffect, useState } from 'react';
import {
  Users,
  Trophy,
  Star,
  BookOpen,
  Plus,
  Check,
  Loader2,
  Flame,
  Award,
  ChevronRight,
} from 'lucide-react';
import { api } from '../api/client';
import BookCard from '../components/BookCard';

export default function CommunityPage({
  user,
  readingMap,
  listVersion,
  onSelectBook,
  onUpsertList,
  onRemoveList,
}) {
  const [subTab, setSubTab] = useState('neighborhood'); // 'neighborhood' | 'favorites'
  const [neighborhood, setNeighborhood] = useState(null);
  const [favorites, setFavorites] = useState(null);
  const [loading, setLoading] = useState(true);

  // Estado del lector vecino seleccionado
  const [selectedNeighborId, setSelectedNeighborId] = useState(null);
  const [neighborTopBooks, setNeighborTopBooks] = useState(null);
  const [loadingTop, setLoadingTop] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([api.getNeighborhood(12), api.getCommunityFavorites(10)])
      .then(([nb, fav]) => {
        if (!cancelled) {
          setNeighborhood(nb);
          setFavorites(fav);
          if (nb?.neighbors?.length > 0 && !selectedNeighborId) {
            setSelectedNeighborId(nb.neighbors[0].user_id);
          }
        }
      })
      .catch((err) => console.error('Error cargando comunidad:', err))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.user_id, listVersion]);

  useEffect(() => {
    if (!selectedNeighborId) return;
    let cancelled = false;
    setLoadingTop(true);
    api
      .getUserTopBooks(selectedNeighborId)
      .then((data) => {
        if (!cancelled) setNeighborTopBooks(data);
      })
      .catch((err) => console.error('Error cargando top 10 vecino:', err))
      .finally(() => {
        if (!cancelled) setLoadingTop(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedNeighborId, listVersion]);

  return (
    <div className="space-y-6">
      {/* Cabecera Comunidad + Selector de subsecciones */}
      <section className="rounded-3xl border border-stone-200/90 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-brand-100 text-brand-700">
              <Users className="h-5 w-5" />
            </div>
            <div>
              <h1 className="font-editorial text-2xl sm:text-3xl font-bold text-stone-900">
                Comunidad de Lectores
              </h1>
              <p className="text-xs sm:text-sm text-stone-500">
                Descubre a los lectores con gustos más parecidos a los tuyos (Top 10 público) y los
                favoritos globales de la plataforma.
              </p>
            </div>
          </div>

          <div className="flex items-center gap-1.5 rounded-2xl bg-stone-100 p-1">
            <button
              onClick={() => setSubTab('neighborhood')}
              className={`inline-flex items-center gap-2 rounded-xl px-4 py-2 text-xs font-semibold transition-all ${subTab === 'neighborhood'
                ? 'bg-white text-brand-700 shadow-sm'
                : 'text-stone-600 hover:text-stone-900'
                }`}
            >
              <Users className="h-4 w-4" />
              Tu Vecindario de Lectores
            </button>
            <button
              onClick={() => setSubTab('favorites')}
              className={`inline-flex items-center gap-2 rounded-xl px-4 py-2 text-xs font-semibold transition-all ${subTab === 'favorites'
                ? 'bg-white text-brand-700 shadow-sm'
                : 'text-stone-600 hover:text-stone-900'
                }`}
            >
              <Trophy className="h-4 w-4" />
              Favoritos de la Comunidad
            </button>
          </div>
        </div>

        {/* Estadísticas globales de la comunidad */}
        {favorites?.community_stats && (
          <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="rounded-2xl bg-stone-50 p-3.5 ring-1 ring-stone-200/70">
              <p className="text-[11px] font-semibold uppercase text-stone-400">Catálogo</p>
              <p className="mt-1 text-xl font-bold text-stone-900">
                {favorites.community_stats.total_books.toLocaleString()}
              </p>
              <p className="text-[11px] text-stone-500">libros curados</p>
            </div>
            <div className="rounded-2xl bg-stone-50 p-3.5 ring-1 ring-stone-200/70">
              <p className="text-[11px] font-semibold uppercase text-stone-400">Lectores</p>
              <p className="mt-1 text-xl font-bold text-stone-900">
                {favorites.community_stats.total_users.toLocaleString()}
              </p>
              <p className="text-[11px] text-stone-500">miembros activos</p>
            </div>
            <div className="rounded-2xl bg-stone-50 p-3.5 ring-1 ring-stone-200/70">
              <p className="text-[11px] font-semibold uppercase text-stone-400">Interacciones</p>
              <p className="mt-1 text-xl font-bold text-stone-900">
                {favorites.community_stats.total_interactions.toLocaleString()}
              </p>
              <p className="text-[11px] text-stone-500">en listas de lectura</p>
            </div>
            <div className="rounded-2xl bg-stone-50 p-3.5 ring-1 ring-stone-200/70">
              <p className="text-[11px] font-semibold uppercase text-stone-400">Media Comunitaria</p>
              <p className="mt-1 text-xl font-bold text-amber-700">
                {favorites.community_stats.community_global_mean}★
              </p>
              <p className="text-[11px] text-stone-500">escala 1–5 estrellas</p>
            </div>
          </div>
        )}
      </section>

      {loading ? (
        <div className="flex items-center justify-center py-16 text-stone-500">
          <Loader2 className="h-7 w-7 animate-spin text-brand-600" />
        </div>
      ) : subTab === 'neighborhood' ? (
        /* SUB-SECCIÓN 1: TU VECINDARIO DE LECTORES + TOP 10 PÚBLICO (RF-16) */
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
          {/* Columna izquierda: lista de vecinos afines */}
          <div className="lg:col-span-5 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="font-editorial text-xl font-bold text-stone-900">
                Lectores más parecidos a ti
              </h2>
              <span className="rounded-full bg-brand-50 px-2.5 py-0.5 text-[11px] font-semibold text-brand-700 ring-1 ring-brand-200">
                {neighborhood?.mode === 'collaborative_msd'
                  ? `Al menos ${neighborhood.min_overlap_used} libros en común`
                  : 'Afinidad por géneros'}
              </span>
            </div>

            <div className="space-y-2.5 max-h-[680px] overflow-y-auto pr-1">
              {(neighborhood?.neighbors || []).map((n) => {
                const selected = selectedNeighborId === n.user_id;
                return (
                  <button
                    key={n.user_id}
                    onClick={() => setSelectedNeighborId(n.user_id)}
                    className={`w-full rounded-2xl border p-4 text-left transition-all ${selected
                      ? 'border-brand-500 bg-brand-50/50 shadow-sm ring-1 ring-brand-500/30'
                      : 'border-stone-200 bg-white hover:border-brand-300 hover:bg-stone-50/70'
                      }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2.5">
                        <div className="flex h-9 w-9 items-center justify-center rounded-full bg-brand-600 text-xs font-bold text-white">
                          {n.display_name.slice(0, 2).toUpperCase()}
                        </div>
                        <div>
                          <p className="text-sm font-bold text-stone-900">{n.display_name}</p>
                          <p className="text-[11px] text-stone-500">
                            @{n.username} · {n.total_books} libros en su lista
                          </p>
                        </div>
                      </div>

                      <div className="flex items-center gap-1.5">
                        <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-xs font-bold text-emerald-800">
                          {n.affinity_percent}% afín
                        </span>
                        <ChevronRight className="h-4 w-4 text-stone-400" />
                      </div>
                    </div>

                    <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                      {n.favorite_genres.map((g) => (
                        <span
                          key={g}
                          className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${n.shared_genres?.includes(g)
                            ? 'bg-brand-600 text-white'
                            : 'bg-stone-100 text-stone-600'
                            }`}
                        >
                          {g}
                        </span>
                      ))}
                    </div>

                    {n.match_type === 'collaborative_msd' && (
                      <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] font-mono text-stone-500">
                        <span>{n.overlap_books} libros en común</span>
                      </div>
                    )}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Columna derecha: Lista Pública Top 10 del Vecino Seleccionado */}
          <div className="lg:col-span-7">
            <div className="rounded-3xl border border-stone-200 bg-white p-5 sm:p-6 shadow-sm">
              {loadingTop || !neighborTopBooks ? (
                <div className="flex flex-col items-center justify-center py-20 text-stone-500">
                  <Loader2 className="h-6 w-6 animate-spin text-brand-600" />
                  <p className="mt-2 text-xs">Cargando Top 10 público del lector...</p>
                </div>
              ) : (
                <>
                  <div className="flex flex-wrap items-center justify-between gap-3 border-b border-stone-100 pb-4">
                    <div>
                      <h3 className="font-editorial text-xl font-bold text-stone-900">
                        Top 10 de {neighborTopBooks.user.display_name}
                      </h3>
                      <p className="text-xs text-stone-500">
                        Mostrando los <strong>{neighborTopBooks.shown_count}</strong> mejores libros
                        (de <strong>{neighborTopBooks.total_books_in_list}</strong> en su lista
                        completa · tope público Top {neighborTopBooks.max_public_limit}).
                      </p>
                    </div>
                  </div>

                  <div className="mt-4 divide-y divide-stone-100">
                    {neighborTopBooks.top_books.map((item) => {
                      const alreadyHave = Boolean(readingMap[item.book_id]) || item.in_viewer_list;
                      return (
                        <div
                          key={item.book_id}
                          onClick={() => onSelectBook(item.book_id)}
                          className="flex flex-wrap items-center justify-between gap-3 py-3.5 hover:bg-stone-50/70 px-2 rounded-xl transition-colors cursor-pointer"
                        >
                          <div className="flex items-start gap-3 min-w-0 flex-1">
                            <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-stone-900 text-xs font-bold text-white">
                              {item.rank}
                            </span>
                            <div className="min-w-0">
                              <p className="font-editorial text-base font-bold text-stone-900 truncate">
                                {item.title}
                              </p>
                              <p className="text-xs text-stone-500 truncate">
                                {item.author} ·{' '}
                                <span className="font-medium text-brand-700">
                                  {item.primary_genre}
                                </span>
                              </p>
                              <div className="mt-1 flex items-center gap-2 text-[11px]">
                                <span className="inline-flex items-center gap-1 rounded-md bg-amber-50 px-2 py-0.5 font-semibold text-amber-800">
                                  <Star className="h-3 w-3 fill-amber-500 text-amber-500" />
                                  Su nota:{' '}
                                  {item.neighbor_rating != null
                                    ? `${item.neighbor_rating}★`
                                    : `${item.neighbor_effective_rating.toFixed(1)}★ (${item.neighbor_status})`}
                                </span>
                              </div>
                            </div>
                          </div>

                          <div onClick={(e) => e.stopPropagation()} className="shrink-0">
                            {alreadyHave ? (
                              <span className="inline-flex items-center gap-1 rounded-lg bg-emerald-50 px-3 py-1.5 text-xs font-semibold text-emerald-700 ring-1 ring-emerald-200">
                                <Check className="h-3.5 w-3.5" />
                                En tu lista
                              </span>
                            ) : (
                              <button
                                onClick={() => onUpsertList(item.book_id, 'por_leer', null)}
                                className="inline-flex items-center gap-1 rounded-lg bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-700 transition-colors"
                              >
                                <Plus className="h-3.5 w-3.5" />
                                Agregar
                              </button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      ) : (
        /* SUB-SECCIÓN 2: FAVORITOS DE LA COMUNIDAD */
        <div className="space-y-10">
          {/* Top 10 Más Leídos */}
          <section className="space-y-4">
            <div className="flex items-center gap-2.5">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-orange-100 text-orange-700">
                <Flame className="h-5 w-5" />
              </div>
              <div>
                <h2 className="font-editorial text-2xl font-bold text-stone-900">
                  Más leídos por la comunidad
                </h2>
                <p className="text-xs text-stone-500">
                  Libros con mayor volumen de lectores y valoraciones en las listas de usuarios.
                </p>
              </div>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {(favorites?.most_read || []).map((book) => (
                <BookCard
                  key={book.book_id}
                  book={book}
                  rankBadge={book.rank}
                  userEntry={readingMap[book.book_id]}
                  extraMeta={
                    <div className="flex items-center justify-between rounded-xl bg-orange-50/70 px-3 py-2 text-xs text-orange-900 ring-1 ring-orange-200/80">
                      <span className="inline-flex items-center gap-1 font-semibold">
                        <BookOpen className="h-3.5 w-3.5" />
                        {book.community_readers} lectores
                      </span>
                      <span className="font-bold">Calificación media: {book.community_avg_rating.toFixed(2)}★</span>
                    </div>
                  }
                  onSelectBook={onSelectBook}
                  onUpsertList={onUpsertList}
                  onRemoveList={onRemoveList}
                />
              ))}
            </div>
          </section>

          {/* Top 10 Mejor Puntuados */}
          <section className="space-y-4 border-t border-stone-200 pt-8">
            <div className="flex items-center gap-2.5">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-amber-100 text-amber-700">
                <Award className="h-5 w-5" />
              </div>
              <div>
                <h2 className="font-editorial text-2xl font-bold text-stone-900">
                  Mejor puntuados por la comunidad
                </h2>
                <p className="text-xs text-stone-500">
                  Ranking ponderado comunitario (mínimo{' '}
                  {favorites?.min_votes_threshold} lectores en la plataforma).
                </p>
              </div>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {(favorites?.highest_rated || []).map((book) => (
                <BookCard
                  key={book.book_id}
                  book={book}
                  rankBadge={book.rank}
                  userEntry={readingMap[book.book_id]}
                  extraMeta={
                    <div className="flex items-center justify-between rounded-xl bg-amber-50/80 px-3 py-2 text-xs text-amber-900 ring-1 ring-amber-200/80">
                      <span className="font-semibold">
                        Comunidad: {book.community_avg_rating.toFixed(2)}★ ({book.community_readers})
                      </span>
                    </div>
                  }
                  onSelectBook={onSelectBook}
                  onUpsertList={onUpsertList}
                  onRemoveList={onRemoveList}
                />
              ))}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
