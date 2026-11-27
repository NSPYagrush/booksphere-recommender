import React, { useEffect, useState } from 'react';
import {
  X,
  Star,
  BookOpen,
  Calendar,
  Hash,
  Users,
  Plus,
  Trash2,
  Loader2,
  Sparkles,
} from 'lucide-react';
import { api } from '../api/client';
import { getGenreStyle } from './BookCard';

const STATUS_OPTIONS = [
  { value: 'por_leer', label: 'Por leer' },
  { value: 'leyendo', label: 'Leyendo' },
  { value: 'leido', label: 'Leído' },
];

export default function BookModal({
  bookId,
  readingMap,
  onClose,
  onSelectBook,
  onUpsertList,
  onRemoveList,
}) {
  const [book, setBook] = useState(null);
  const [similarData, setSimilarData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!bookId) return;
    let cancelled = false;
    setLoading(true);
    setBook(null);
    setSimilarData(null);

    Promise.all([
      api.getBookDetail(bookId),
      api.getSimilarBooks(bookId, 6, 5),
    ])
      .then(([detail, sim]) => {
        if (!cancelled) {
          setBook(detail);
          setSimilarData(sim);
        }
      })
      .catch((err) => {
        console.error('Error cargando detalle del libro:', err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [bookId]);

  if (!bookId) return null;

  const entry = readingMap?.[bookId] || book?.user_entry || null;
  const inList = Boolean(entry);
  const currentStatus = entry?.status || 'por_leer';
  const currentRating = entry?.rating ?? null;
  const effectiveRating = entry?.effective_rating ?? null;
  const isImplicit = entry?.is_implicit ?? entry?.is_implicit_rating ?? false;

  const handleSave = async (statusVal, ratingVal) => {
    setBusy(true);
    try {
      await onUpsertList(bookId, statusVal, ratingVal);
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    setBusy(true);
    try {
      await onRemoveList(bookId);
    } finally {
      setBusy(false);
    }
  };

  const style = getGenreStyle(book?.primary_genre);

  return (
    <div
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center bg-stone-900/55 p-4 backdrop-blur-sm"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl ring-1 ring-stone-200"
      >
        {/* Franja superior por género */}
        <div className={`h-2 w-full ${style.bar}`} />

        <button
          onClick={onClose}
          className="absolute right-4 top-5 z-10 rounded-full bg-stone-100 p-2 text-stone-600 hover:bg-stone-200 hover:text-stone-900 transition-colors"
          aria-label="Cerrar"
        >
          <X className="h-5 w-5" />
        </button>

        <div className="overflow-y-auto p-6 sm:p-8">
          {loading || !book ? (
            <div className="flex flex-col items-center justify-center py-16 text-stone-500">
              <Loader2 className="h-8 w-8 animate-spin text-brand-600" />
              <p className="mt-3 text-sm font-medium">
                Cargando ficha del libro y calculando vecinos Ítem-Ítem (MSD)...
              </p>
            </div>
          ) : (
            <>
              {/* Ficha principal */}
              <div className="pr-10">
                <div className="flex flex-wrap items-center gap-2">
                  {book.genres?.map((g) => (
                    <span
                      key={g}
                      className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ring-1 ring-inset ${
                        g === book.primary_genre
                          ? style.badge
                          : 'bg-stone-100 text-stone-600 ring-stone-200'
                      }`}
                    >
                      {g}
                    </span>
                  ))}
                </div>

                <h2 className="mt-3 font-editorial text-2xl sm:text-3xl font-bold text-stone-900">
                  {book.title}
                </h2>
                <p className="mt-1 text-base font-medium text-stone-600">
                  por <span className="text-stone-900">{book.author}</span>
                </p>

                {/* Metadatos numéricos */}
                <div className="mt-4 flex flex-wrap items-center gap-4 text-xs font-medium text-stone-600">
                  {book.avg_rating != null ? (
                    <span className="inline-flex items-center gap-1 rounded-lg bg-amber-50 px-2.5 py-1 text-amber-800 ring-1 ring-amber-200">
                      <Star className="h-3.5 w-3.5 fill-amber-500 text-amber-500" />
                      <strong>{Number(book.avg_rating).toFixed(2)}</strong>
                      <span className="text-amber-700/80">
                        ({book.ratings_count?.toLocaleString()} valoraciones)
                      </span>
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 rounded-lg bg-stone-100 px-2.5 py-1 text-stone-600 ring-1 ring-stone-200">
                      Sin puntuación global
                    </span>
                  )}
                  {book.publish_year && (
                    <span className="inline-flex items-center gap-1">
                      <Calendar className="h-3.5 w-3.5 text-stone-400" />
                      Año: {book.publish_year}
                    </span>
                  )}
                  {book.pages && (
                    <span className="inline-flex items-center gap-1">
                      <BookOpen className="h-3.5 w-3.5 text-stone-400" />
                      {book.pages} páginas
                    </span>
                  )}
                  {book.isbn && (
                    <span className="inline-flex items-center gap-1">
                      <Hash className="h-3.5 w-3.5 text-stone-400" />
                      ISBN: {book.isbn}
                    </span>
                  )}
                </div>
              </div>

              {/* Panel de acciones de lista dinámica */}
              <div className="mt-5 rounded-2xl border border-brand-200/80 bg-brand-50/50 p-4">
                {!inList ? (
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <p className="text-sm font-semibold text-stone-800">
                        ¿Te interesa este libro?
                      </p>
                      <p className="text-xs text-stone-600">
                        Al agregarlo a tu lista de lectura el recomendador híbrido ajustará tus sugerencias al instante.
                      </p>
                    </div>
                    <button
                      disabled={busy}
                      onClick={() => handleSave('por_leer', null)}
                      className="inline-flex items-center gap-2 rounded-xl bg-brand-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-brand-700 transition-colors"
                    >
                      <Plus className="h-4 w-4" />
                      Agregar a mi lista
                    </button>
                  </div>
                ) : (
                  <div className="flex flex-wrap items-center justify-between gap-4">
                    <div className="flex flex-wrap items-center gap-3">
                      <label className="text-xs font-semibold uppercase tracking-wide text-brand-800">
                        Estado:
                      </label>
                      <select
                        disabled={busy}
                        value={currentStatus}
                        onChange={(e) => handleSave(e.target.value, currentRating)}
                        className="rounded-xl border border-brand-300 bg-white px-3 py-1.5 text-sm font-semibold text-stone-800 focus:border-brand-600 focus:outline-none"
                      >
                        {STATUS_OPTIONS.map((opt) => (
                          <option key={opt.value} value={opt.value}>
                            {opt.label}
                          </option>
                        ))}
                      </select>

                      <div className="flex items-center gap-1 rounded-xl bg-white px-3 py-1.5 ring-1 ring-stone-200">
                        {[1, 2, 3, 4, 5].map((star) => {
                          const active = currentRating != null && star <= currentRating;
                          return (
                            <button
                              key={star}
                              disabled={busy}
                              onClick={() =>
                                handleSave(currentStatus, currentRating === star ? null : star)
                              }
                              className="p-0.5 hover:scale-110 transition-transform"
                            >
                              <Star
                                className={`h-5 w-5 ${
                                  active
                                    ? 'fill-amber-400 text-amber-500'
                                    : 'text-stone-300 hover:text-amber-400'
                                }`}
                              />
                            </button>
                          );
                        })}
                        <span className="ml-2 text-xs font-medium text-stone-600">
                          {isImplicit
                            ? `sin calificación`
                            : `Calificado: ${currentRating}★`}
                        </span>
                      </div>
                    </div>

                    <button
                      disabled={busy}
                      onClick={handleDelete}
                      className="inline-flex items-center gap-1.5 rounded-xl border border-rose-200 bg-white px-3 py-1.5 text-xs font-semibold text-rose-600 hover:bg-rose-50 transition-colors"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                      Quitar
                    </button>
                  </div>
                )}
              </div>

              {/* Sinopsis completa */}
              <div className="mt-6">
                <h3 className="text-xs font-bold uppercase tracking-wider text-stone-400">
                  Sinopsis
                </h3>
                <p className="mt-2 whitespace-pre-line text-sm leading-relaxed text-stone-700">
                  {book.synopsis || 'Sin sinopsis disponible.'}
                </p>
              </div>

              {/* Ubicación 3 del Recomendador: Lectores similares también agregaron... */}
              <div className="mt-8 border-t border-stone-200 pt-6">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h3 className="flex items-center gap-2 font-editorial text-xl font-bold text-stone-900">
                      <Sparkles className="h-5 w-5 text-indigo-600" />
                      Lectores similares también agregaron...
                    </h3>
                    
                  </div>
                  {similarData && (
                    <span className="rounded-full bg-indigo-50 px-3 py-1 text-xs font-semibold text-indigo-700 ring-1 ring-indigo-200">
                      {similarData.collaborative_candidates} lectores similares
                    </span>
                  )}
                </div>

                {similarData?.similar_books?.length > 0 ? (
                  <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
                    {similarData.similar_books.map((sb) => {
                      const sim = sb.similarity;
                      const isAdded = Boolean(readingMap?.[sb.book_id]);
                      return (
                        <div
                          key={sb.book_id}
                          onClick={() => onSelectBook(sb.book_id)}
                          className="flex flex-col justify-between rounded-xl border border-stone-200 bg-stone-50/60 p-3.5 hover:border-indigo-300 hover:bg-white transition-all cursor-pointer"
                        >
                          <div>
                            <div className="flex items-center justify-between gap-2">
                              <span className="rounded-full bg-white px-2 py-0.5 text-[11px] font-semibold text-stone-600 ring-1 ring-stone-200">
                                {sb.primary_genre}
                              </span>
                              <span className="inline-flex items-center gap-1 rounded-full bg-indigo-600 px-2 py-0.5 text-[11px] font-bold text-white">
                                <Users className="h-3 w-3" />
                                {sim.match_percent}%
                              </span>
                            </div>
                            <h4 className="mt-2 font-editorial text-base font-bold text-stone-900 line-clamp-1">
                              {sb.title}
                            </h4>
                            <p className="text-xs text-stone-500 line-clamp-1">{sb.author}</p>
                            <p className="mt-1.5 text-[11px] text-stone-600">{sim.reason}</p>
                          </div>

                          <div
                            onClick={(e) => e.stopPropagation()}
                            className="mt-3 flex items-center justify-between border-t border-stone-200/70 pt-2 text-[11px]"
                          >
                            {isAdded ? (
                              <span className="font-semibold text-emerald-700">En tu lista</span>
                            ) : (
                              <button
                                onClick={() => onUpsertList(sb.book_id, 'por_leer', null)}
                                className="font-semibold text-brand-700 hover:underline"
                              >
                                + Agregar
                              </button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="mt-4 text-sm text-stone-500">
                    No se encontraron libros similares para este título.
                  </p>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
