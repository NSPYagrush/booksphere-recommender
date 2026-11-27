import React, { useState } from 'react';
import {
  Star,
  Plus,
  Trash2,
  Sparkles,
  Users,
  BookMarked,
  Info,
} from 'lucide-react';

// Paleta tipográfica editorial por género
const GENRE_STYLES = {
  Fantasy: { bar: 'bg-violet-600', badge: 'bg-violet-50 text-violet-700 ring-violet-200' },
  'Science Fiction': { bar: 'bg-cyan-600', badge: 'bg-cyan-50 text-cyan-800 ring-cyan-200' },
  Classics: { bar: 'bg-amber-700', badge: 'bg-amber-50 text-amber-800 ring-amber-200' },
  Mystery: { bar: 'bg-slate-700', badge: 'bg-slate-100 text-slate-800 ring-slate-300' },
  Thriller: { bar: 'bg-rose-700', badge: 'bg-rose-50 text-rose-800 ring-rose-200' },
  'Historical Fiction': { bar: 'bg-orange-700', badge: 'bg-orange-50 text-orange-800 ring-orange-200' },
  Romance: { bar: 'bg-pink-600', badge: 'bg-pink-50 text-pink-700 ring-pink-200' },
  'Young Adult': { bar: 'bg-fuchsia-600', badge: 'bg-fuchsia-50 text-fuchsia-700 ring-fuchsia-200' },
  Horror: { bar: 'bg-red-800', badge: 'bg-red-50 text-red-800 ring-red-200' },
  History: { bar: 'bg-yellow-700', badge: 'bg-yellow-50 text-yellow-800 ring-yellow-200' },
  Science: { bar: 'bg-teal-600', badge: 'bg-teal-50 text-teal-800 ring-teal-200' },
  'Biography & Memoir': { bar: 'bg-emerald-700', badge: 'bg-emerald-50 text-emerald-800 ring-emerald-200' },
  Psychology: { bar: 'bg-indigo-600', badge: 'bg-indigo-50 text-indigo-700 ring-indigo-200' },
  Philosophy: { bar: 'bg-stone-700', badge: 'bg-stone-100 text-stone-800 ring-stone-300' },
  Business: { bar: 'bg-blue-700', badge: 'bg-blue-50 text-blue-800 ring-blue-200' },
};

const DEFAULT_STYLE = {
  bar: 'bg-brand-600',
  badge: 'bg-brand-50 text-brand-700 ring-brand-200',
};

const STATUS_OPTIONS = [
  { value: 'por_leer', label: 'Por leer', implicit: '3.0★' },
  { value: 'leyendo', label: 'Leyendo', implicit: '3.7★' },
  { value: 'leido', label: 'Leído', implicit: '4.0★' },
];

export function getGenreStyle(primaryGenre) {
  return GENRE_STYLES[primaryGenre] || DEFAULT_STYLE;
}

export default function BookCard({
  book,
  userEntry,
  recommendation,
  similarity,
  rankBadge,
  extraMeta,
  onSelectBook,
  onUpsertList,
  onRemoveList,
}) {
  const [busy, setBusy] = useState(false);
  const style = getGenreStyle(book.primary_genre);

  const entry = userEntry ?? book.user_entry ?? null;
  const inList = Boolean(entry);
  const currentStatus = entry?.status || 'por_leer';
  const currentRating = entry?.rating ?? null;
  const effectiveRating = entry?.effective_rating ?? null;
  const isImplicit = entry?.is_implicit ?? entry?.is_implicit_rating ?? false;

  const handleQuickAdd = async (e) => {
    e.stopPropagation();
    if (!onUpsertList || busy) return;
    setBusy(true);
    try {
      await onUpsertList(book.book_id, 'por_leer', null);
    } finally {
      setBusy(false);
    }
  };

  const handleStatusChange = async (e) => {
    e.stopPropagation();
    if (!onUpsertList || busy) return;
    const nextStatus = e.target.value;
    setBusy(true);
    try {
      await onUpsertList(book.book_id, nextStatus, currentRating);
    } finally {
      setBusy(false);
    }
  };

  const handleStarClick = async (e, stars) => {
    e.stopPropagation();
    if (!onUpsertList || busy) return;
    // Si hace clic de nuevo en la misma estrella, limpia la nota explícita (vuelve a implícita)
    const nextRating = currentRating === stars ? null : stars;
    setBusy(true);
    try {
      await onUpsertList(book.book_id, currentStatus, nextRating);
    } finally {
      setBusy(false);
    }
  };

  const handleRemove = async (e) => {
    e.stopPropagation();
    if (!onRemoveList || busy) return;
    setBusy(true);
    try {
      await onRemoveList(book.book_id);
    } finally {
      setBusy(false);
    }
  };

  return (
    <article
      onClick={() => onSelectBook && onSelectBook(book.book_id)}
      className="group relative flex flex-col overflow-hidden rounded-2xl border border-stone-200/90 bg-white shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:border-brand-300 hover:shadow-md cursor-pointer"
    >
      {/* Lomo superior editorial coloreado por género */}
      <div className={`h-1.5 w-full ${style.bar}`} />

      <div className="flex flex-1 flex-col p-4 sm:p-5">
        {/* Encabezado de metadatos */}
        <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            {rankBadge && (
              <span className="inline-flex h-6 min-w-[1.5rem] items-center justify-center rounded-md bg-stone-900 px-1.5 text-xs font-bold text-white">
                #{rankBadge}
              </span>
            )}
            <span
              className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ring-1 ring-inset ${style.badge}`}
            >
              {book.primary_genre}
            </span>
            {book.publish_year && (
              <span className="text-xs font-medium text-stone-400">
                {book.publish_year}
              </span>
            )}
          </div>

          {/* Rating global + badge de afinidad si es recomendación */}
          <div className="flex items-center gap-1.5">
            {recommendation && (
              <span className="inline-flex items-center gap-1 rounded-full bg-brand-600 px-2.5 py-0.5 text-xs font-bold text-white">
                <Sparkles className="h-3 w-3" />
                {recommendation.match_percent}%
              </span>
            )}
            {similarity && (
              <span className="inline-flex items-center gap-1 rounded-full bg-indigo-600 px-2.5 py-0.5 text-xs font-bold text-white">
                <Users className="h-3 w-3" />
                {similarity.match_percent}%
              </span>
            )}
            {book.avg_rating != null && (
              <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-800 ring-1 ring-amber-200/80">
                <Star className="h-3 w-3 fill-amber-500 text-amber-500" />
                {Number(book.avg_rating).toFixed(2)}
              </span>
            )}
          </div>
        </div>

        {/* Título y autor */}
        <h3 className="font-editorial text-lg font-bold leading-snug text-stone-900 group-hover:text-brand-700 transition-colors line-clamp-2">
          {book.title}
        </h3>
        <p className="mt-0.5 text-xs font-medium text-stone-500 line-clamp-1">
          por <span className="text-stone-700">{book.author}</span>
        </p>

        {/* Sinopsis corta */}
        {book.synopsis_snippet && (
          <p className="mt-2.5 text-xs leading-relaxed text-stone-600 line-clamp-3">
            {book.synopsis_snippet}
          </p>
        )}

        {/* Bloque de explicabilidad */}
        {(recommendation || similarity) && (
          <div className="mt-3 rounded-xl bg-stone-50 px-3 py-2 text-[11px] leading-snug text-stone-600 ring-1 ring-stone-200/70">
            <div className="flex items-start gap-1.5">
              <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-brand-600" />
              <span>{recommendation?.reason || similarity?.reason}</span>
            </div>

            {recommendation?.components && recommendation.mode === 'hybrid' && (() => {
              const cfPct = Math.min(100, Math.max(0, Math.round((recommendation.components.cf ?? 0) * 100)));
              const contentPct = Math.min(100, Math.max(0, Math.round((recommendation.components.content ?? 0) * 100)));

              return (
                <div className="mt-2 space-y-1.5 border-t border-stone-200/70 pt-2 text-[10px] text-stone-600">
                  <div className="grid grid-cols-2 gap-2">
                    <div title="Afinidad según lectores con historial parecido al tuyo">
                      <div className="flex items-center justify-between font-medium">
                        <span>Lectores afines</span>
                        <span className="font-semibold text-stone-800">{cfPct}%</span>
                      </div>
                      <div className="mt-0.5 h-1 w-full overflow-hidden rounded-full bg-stone-200">
                        <div
                          className="h-full rounded-full bg-brand-600"
                          style={{ width: `${cfPct}%` }}
                        />
                      </div>
                    </div>

                    <div title="Similitud por género, temas y sinopsis con tus lecturas">
                      <div className="flex items-center justify-between font-medium">
                        <span>Temas similares</span>
                        <span className="font-semibold text-stone-800">{contentPct}%</span>
                      </div>
                      <div className="mt-0.5 h-1 w-full overflow-hidden rounded-full bg-stone-200">
                        <div
                          className="h-full rounded-full bg-amber-500"
                          style={{ width: `${contentPct}%` }}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              );
            })()}

            {similarity && similarity.sim_msd_norm != null && (
              <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[10px] font-medium text-indigo-700">
                <span>sim_MSD: {similarity.sim_msd_norm.toFixed(2)}</span>
                <span>·</span>
                <span>JMSD: {similarity.jmsd?.toFixed(3)}</span>
                <span>·</span>
                <span>|U_ij|={similarity.overlap_users}</span>
              </div>
            )}
          </div>
        )}

        {/* Metadatos comunitarios opcionales (Favoritos / Top 10 vecino) */}
        {extraMeta && <div className="mt-2.5">{extraMeta}</div>}

        <div className="flex-1" />

        {/* Pie interactivo: Lista de lectura dinámica + 1-5 estrellas */}
        <div
          onClick={(e) => e.stopPropagation()}
          className="mt-4 border-t border-stone-100 pt-3"
        >
          {!inList ? (
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-stone-400">
                {book.ratings_count
                  ? `${book.ratings_count.toLocaleString()} valoraciones`
                  : book.avg_rating == null
                    ? 'Sin puntuación global'
                    : 'Ver detalle'}
              </span>
              {onUpsertList && (
                <button
                  disabled={busy}
                  onClick={handleQuickAdd}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm hover:bg-brand-700 disabled:opacity-50 transition-colors"
                >
                  <Plus className="h-3.5 w-3.5" />
                  Agregar a mi lista
                </button>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-1.5">
                  <BookMarked className="h-3.5 w-3.5 text-brand-600" />
                  <select
                    disabled={busy}
                    value={currentStatus}
                    onChange={handleStatusChange}
                    className="rounded-lg border border-brand-200 bg-brand-50/70 px-2 py-1 text-xs font-semibold text-brand-800 focus:border-brand-500 focus:outline-none"
                  >
                    {STATUS_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </div>

                {onRemoveList && (
                  <button
                    disabled={busy}
                    onClick={handleRemove}
                    title="Quitar de mi lista"
                    className="rounded-md p-1 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition-colors"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>

              {/* Calificación explícita 1-5 estrellas o voto implícito temporal */}
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-0.5" title="Calificar de 1 a 5 estrellas">
                  {[1, 2, 3, 4, 5].map((star) => {
                    const active = currentRating != null && star <= currentRating;
                    return (
                      <button
                        key={star}
                        disabled={busy}
                        onClick={(e) => handleStarClick(e, star)}
                        className="p-0.5 transition-transform hover:scale-110 focus:outline-none"
                        aria-label={`${star} estrellas`}
                      >
                        <Star
                          className={`h-4 w-4 ${active
                              ? 'fill-amber-400 text-amber-500'
                              : 'text-stone-300 hover:text-amber-400'
                            }`}
                        />
                      </button>
                    );
                  })}
                </div>

                <span
                  className={`text-[11px] font-medium ${isImplicit || currentRating == null ? 'text-stone-500' : 'text-amber-700'
                    }`}
                >
                  {isImplicit || currentRating == null
                    ? `Aún no has calificado éste libro`
                    : `Tu nota: ${currentRating}★`}
                </span>
              </div>
            </div>
          )}
        </div>
      </div>
    </article>
  );
}
