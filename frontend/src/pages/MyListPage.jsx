import React, { useEffect, useState } from 'react';
import {
  Library,
  BookMarked,
  Clock,
  CheckCircle2,
  Sparkles,
  Info,
  Loader2,
  Check,
} from 'lucide-react';
import { api } from '../api/client';
import BookCard from '../components/BookCard';

const STATUS_TABS = [
  { id: '', label: 'Todos', icon: Library },
  { id: 'por_leer', label: 'Por leer', icon: Clock },
  { id: 'leyendo', label: 'Leyendo', icon: BookMarked },
  { id: 'leido', label: 'Leído', icon: CheckCircle2 },
];

export default function MyListPage({
  user,
  genres,
  readingMap,
  listVersion,
  onSelectBook,
  onUpsertList,
  onRemoveList,
  onUpdateFavoriteGenres,
}) {
  const [statusFilter, setStatusFilter] = useState('');
  const [listData, setListData] = useState(null);
  const [loading, setLoading] = useState(true);

  const [editingGenres, setEditingGenres] = useState(false);
  const [draftGenres, setDraftGenres] = useState(user?.favorite_genres || []);
  const [savingGenres, setSavingGenres] = useState(false);

  useEffect(() => {
    setDraftGenres(user?.favorite_genres || []);
  }, [user?.favorite_genres]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getReadingList(statusFilter)
      .then((data) => {
        if (!cancelled) setListData(data);
      })
      .catch((err) => console.error('Error cargando lista de lectura:', err))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [statusFilter, listVersion]);

  const counts = listData?.counts_by_status || { por_leer: 0, leyendo: 0, leido: 0 };
  const total = listData?.total || 0;

  const toggleDraftGenre = (g) => {
    setDraftGenres((prev) =>
      prev.includes(g) ? prev.filter((x) => x !== g) : prev.length < 6 ? [...prev, g] : prev
    );
  };

  const handleSaveGenres = async () => {
    if (draftGenres.length === 0) return;
    setSavingGenres(true);
    try {
      await onUpdateFavoriteGenres(draftGenres);
      setEditingGenres(false);
    } finally {
      setSavingGenres(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Cabecera de Mi Lista Dinámica */}
      <section className="rounded-3xl border border-stone-200/90 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="font-editorial text-2xl sm:text-3xl font-bold text-stone-900">
              Mi Lista de Lectura
            </h1>
            <p className="mt-1 text-xs sm:text-sm text-stone-500">
              Cambia el estado de tus libros entre <strong>Por leer</strong>,{' '}
              <strong>Leyendo</strong> y <strong>Leído</strong>, y asigna tu calificación de 1 a 5
              estrellas.
            </p>
          </div>

          <button
            onClick={() => setEditingGenres((v) => !v)}
            className="rounded-xl border border-stone-200 bg-stone-50 px-3.5 py-2 text-xs font-semibold text-stone-700 hover:bg-stone-100"
          >
            {editingGenres ? 'Cancelar edición de géneros' : 'Editar géneros favoritos'}
          </button>
        </div>

        {/* Géneros favoritos del perfil */}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold text-stone-500">Géneros del perfil:</span>
          {(user?.favorite_genres || []).map((g) => (
            <span
              key={g}
              className="rounded-full bg-brand-50 px-2.5 py-0.5 text-xs font-semibold text-brand-700 ring-1 ring-brand-200"
            >
              {g}
            </span>
          ))}
        </div>

        {editingGenres && (
          <div className="mt-4 rounded-2xl border border-brand-200 bg-brand-50/40 p-4 space-y-3">
            <p className="text-xs font-semibold text-brand-900">
              Selecciona entre 1 y 6 géneros favoritos:
            </p>
            <div className="flex flex-wrap gap-2">
              {genres
                .filter((g) => !['Fiction', 'Nonfiction'].includes(g.genre))
                .map((g) => {
                  const active = draftGenres.includes(g.genre);
                  return (
                    <button
                      key={g.genre}
                      onClick={() => toggleDraftGenre(g.genre)}
                      className={`inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs font-medium ${active
                        ? 'bg-brand-600 text-white'
                        : 'bg-white text-stone-700 ring-1 ring-stone-200'
                        }`}
                    >
                      {active && <Check className="h-3 w-3" />}
                      {g.genre}
                    </button>
                  );
                })}
            </div>
            <button
              disabled={savingGenres || draftGenres.length === 0}
              onClick={handleSaveGenres}
              className="rounded-xl bg-brand-600 px-4 py-2 text-xs font-semibold text-white hover:bg-brand-700 disabled:opacity-50"
            >
              {savingGenres ? 'Guardando...' : 'Guardar géneros favoritos'}
            </button>
          </div>
        )}

        {/* Pestañas de estado */}
        <div className="mt-5 flex flex-wrap gap-2">
          {STATUS_TABS.map(({ id, label, icon: Icon, implicit }) => {
            const active = statusFilter === id;
            const count = id === '' ? total : counts[id] || 0;
            return (
              <button
                key={id || 'all'}
                onClick={() => setStatusFilter(id)}
                className={`inline-flex items-center gap-2 rounded-xl px-4 py-2 text-xs font-semibold transition-all ${active
                  ? 'bg-brand-600 text-white shadow-sm'
                  : 'bg-stone-100 text-stone-700 hover:bg-stone-200'
                  }`}
              >
                <Icon className="h-3.5 w-3.5" />
                <span>{label}</span>
                <span
                  className={`rounded-full px-2 py-0.5 text-[11px] ${active ? 'bg-white/20 text-white' : 'bg-white text-stone-600'
                    }`}
                >
                  {count}
                </span>
                {implicit && !active && (
                  <span className="text-[10px] text-stone-400">({implicit})</span>
                )}
              </button>
            );
          })}
        </div>
      </section>

      {/* Grid de libros en la lista */}
      {loading ? (
        <div className="flex items-center justify-center py-16 text-stone-500">
          <Loader2 className="h-7 w-7 animate-spin text-brand-600" />
        </div>
      ) : listData?.items?.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {listData.items.map((item) => (
            <BookCard
              key={item.book_id}
              book={item}
              userEntry={
                readingMap[item.book_id] || {
                  status: item.status,
                  rating: item.rating,
                  effective_rating: item.effective_rating,
                  is_implicit: item.is_implicit_rating,
                }
              }
              onSelectBook={onSelectBook}
              onUpsertList={onUpsertList}
              onRemoveList={onRemoveList}
            />
          ))}
        </div>
      ) : (
        <div className="rounded-3xl border border-dashed border-stone-300 bg-white/80 p-12 text-center">
          <Sparkles className="mx-auto h-8 w-8 text-brand-500" />
          <h3 className="mt-3 font-editorial text-xl font-bold text-stone-800">
            {statusFilter
              ? 'No tienes libros en este estado todavía'
              : 'Tu lista de lectura está vacía'}
          </h3>
          <p className="mt-1 text-xs text-stone-500">
            Ve a <strong>Inicio y Buscador</strong> o a la pestaña de <strong>Géneros</strong> y
            agrega tus primeros libros para activar las recomendaciones híbridas.
          </p>
        </div>
      )}
    </div>
  );
}
