import React, { useEffect, useState } from 'react';
import {
  BookOpen,
  LogIn,
  UserPlus,
  Sparkles,
  Check,
  AlertCircle,
  Loader2,
} from 'lucide-react';
import { api } from '../api/client';

export default function AuthPage({ onAuthenticated }) {
  const [mode, setMode] = useState('login'); // 'login' | 'register'
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [availableGenres, setAvailableGenres] = useState([]);
  const [selectedGenres, setSelectedGenres] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    api
      .getGenres()
      .then((data) => {
        const filtered = (data.genres || []).filter(
          (g) => !['Fiction', 'Nonfiction'].includes(g.genre)
        );
        setAvailableGenres(filtered);
      })
      .catch(() => { });
  }, []);

  const toggleGenre = (genre) => {
    setSelectedGenres((prev) =>
      prev.includes(genre)
        ? prev.filter((g) => g !== genre)
        : prev.length < 6
          ? [...prev, genre]
          : prev
    );
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    if (mode === 'register' && selectedGenres.length === 0) {
      setError('Selecciona al menos 1 género favorito para inicializar tu perfil.');
      return;
    }

    setLoading(true);
    try {
      const res =
        mode === 'login'
          ? await api.login({ username: username.trim(), password })
          : await api.register({
            username: username.trim(),
            password,
            display_name: displayName.trim() || username.trim(),
            favorite_genres: selectedGenres,
          });
      onAuthenticated(res.token, res.user);
    } catch (err) {
      setError(err.message || 'Ocurrió un error al autenticar.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-paper via-stone-50 to-brand-50/40 flex flex-col justify-center px-4 py-10">
      <div className="mx-auto w-full max-w-5xl grid grid-cols-1 lg:grid-cols-12 gap-8 items-center">
        {/* Columna Editorial Izquierda */}
        <div className="lg:col-span-5 space-y-5">
          <div className="inline-flex items-center gap-2 rounded-full bg-brand-100/80 px-3.5 py-1.5 text-xs font-semibold text-brand-800">
            <Sparkles className="h-3.5 w-3.5" />
            Web Social · Recomendador de contenido
          </div>

          <div className="flex items-center gap-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-600 text-white shadow-md">
              <BookOpen className="h-6 w-6" />
            </div>
            <div>
              <h1 className="font-editorial text-4xl font-bold tracking-tight text-stone-900">
                BookSphere
              </h1>
              <p className="text-sm font-medium text-stone-500">
                Comunidad inteligente de descubrimiento literario
              </p>
            </div>
          </div>

          <p className="text-sm leading-relaxed text-stone-600 text-justify">
            Explora un catálogo curado de más de <strong>15,000 libros</strong> con nuestro buscador de <strong>Recuperación de Información</strong>, organiza tu lista dinámica de lectura y recibe recomendaciones personalizadas impulsadas por filtrado colaborativo y análisis de contenido.
          </p>

          {/* <div className="grid grid-cols-1 gap-2.5 pt-2 text-xs text-stone-600">
            <div className="flex items-start gap-2.5 rounded-xl bg-white/80 p-3 ring-1 ring-stone-200/80">
              <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-brand-600 text-[10px] font-bold text-white">
                1
              </span>
              <span>
                <strong>Arranque en frío inteligente:</strong> crea tu cuenta eligiendo tus géneros
                favoritos y obtén recomendaciones inmediatas desde el primer segundo.
              </span>
            </div>
            <div className="flex items-start gap-2.5 rounded-xl bg-white/80 p-3 ring-1 ring-stone-200/80">
              <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-brand-600 text-[10px] font-bold text-white">
                2
              </span>
              <span>
                <strong>Lista de lectura dinámica:</strong> marca libros como <em>Por leer</em>,{' '}
                <em>Leyendo</em> o <em>Leído</em> y califícalos de 1 a 5 estrellas para refinar tu
                vecindario en vivo.
              </span>
            </div>
          </div> */}
        </div>

        {/* Tarjeta de Login / Registro */}
        <div className="lg:col-span-7">
          <div className="overflow-hidden rounded-3xl border border-stone-200 bg-white shadow-xl">
            {/* Switch Login / Registro */}
            <div className="grid grid-cols-2 border-b border-stone-200 bg-stone-50/70 p-1.5">
              <button
                type="button"
                onClick={() => {
                  setMode('login');
                  setError('');
                }}
                className={`flex items-center justify-center gap-2 rounded-2xl py-2.5 text-sm font-semibold transition-all ${mode === 'login'
                  ? 'bg-white text-brand-700 shadow-sm ring-1 ring-stone-200'
                  : 'text-stone-500 hover:text-stone-800'
                  }`}
              >
                <LogIn className="h-4 w-4" />
                Iniciar sesión
              </button>
              <button
                type="button"
                onClick={() => {
                  setMode('register');
                  setError('');
                }}
                className={`flex items-center justify-center gap-2 rounded-2xl py-2.5 text-sm font-semibold transition-all ${mode === 'register'
                  ? 'bg-white text-brand-700 shadow-sm ring-1 ring-stone-200'
                  : 'text-stone-500 hover:text-stone-800'
                  }`}
              >
                <UserPlus className="h-4 w-4" />
                Crear cuenta nueva
              </button>
            </div>

            <form onSubmit={handleSubmit} className="p-6 sm:p-8 space-y-5">
              <div>
                <h2 className="font-editorial text-2xl font-bold text-stone-900">
                  {mode === 'login' ? 'Bienvenido de nuevo' : 'Únete a la comunidad'}
                </h2>
                <p className="mt-1 text-xs text-stone-500">
                  {mode === 'login'
                    ? 'Ingresa tus credenciales para acceder a tu lista y recomendaciones personalizadas.'
                    : 'Crea tu cuenta desde cero y elige tus géneros literarios preferidos.'}
                </p>
              </div>

              {error && (
                <div className="flex items-start gap-2 rounded-xl bg-rose-50 px-3.5 py-3 text-xs font-medium text-rose-700 ring-1 ring-rose-200">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{error}</span>
                </div>
              )}

              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div className={mode === 'login' ? 'sm:col-span-2' : ''}>
                  <label className="block text-xs font-semibold uppercase tracking-wide text-stone-600">
                    Nombre de usuario
                  </label>
                  <input
                    type="text"
                    required
                    minLength={3}
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder="ej. ana_lectora"
                    className="mt-1.5 w-full rounded-xl border border-stone-300 bg-stone-50/40 px-3.5 py-2.5 text-sm text-stone-900 placeholder:text-stone-400 focus:border-brand-500 focus:bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20"
                  />
                </div>

                {mode === 'register' && (
                  <div>
                    <label className="block text-xs font-semibold uppercase tracking-wide text-stone-600">
                      Nombre visible
                    </label>
                    <input
                      type="text"
                      value={displayName}
                      onChange={(e) => setDisplayName(e.target.value)}
                      placeholder="ej. Ana María"
                      className="mt-1.5 w-full rounded-xl border border-stone-300 bg-stone-50/40 px-3.5 py-2.5 text-sm text-stone-900 placeholder:text-stone-400 focus:border-brand-500 focus:bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20"
                    />
                  </div>
                )}

                <div className="sm:col-span-2">
                  <label className="block text-xs font-semibold uppercase tracking-wide text-stone-600">
                    Contraseña
                  </label>
                  <input
                    type="password"
                    required
                    minLength={4}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="••••••••"
                    className="mt-1.5 w-full rounded-xl border border-stone-300 bg-stone-50/40 px-3.5 py-2.5 text-sm text-stone-900 placeholder:text-stone-400 focus:border-brand-500 focus:bg-white focus:outline-none focus:ring-2 focus:ring-brand-500/20"
                  />
                </div>
              </div>

              {/* Selección de géneros favoritos (Cold Start) */}
              {mode === 'register' && (
                <div>
                  <div className="flex items-center justify-between">
                    <label className="text-xs font-semibold uppercase tracking-wide text-stone-600">
                      Tus géneros favoritos ({selectedGenres.length}/6)
                    </label>
                    <span className="text-[11px] text-stone-400">
                      Mínimo 1 · Recomendado 3
                    </span>
                  </div>
                  <div className="mt-2.5 flex flex-wrap gap-2 max-h-44 overflow-y-auto rounded-2xl border border-stone-200 bg-stone-50/60 p-3">
                    {availableGenres.map((g) => {
                      const active = selectedGenres.includes(g.genre);
                      return (
                        <button
                          key={g.genre}
                          type="button"
                          onClick={() => toggleGenre(g.genre)}
                          className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition-all ${active
                            ? 'bg-brand-600 text-white shadow-sm'
                            : 'bg-white text-stone-700 ring-1 ring-stone-200 hover:bg-brand-50 hover:text-brand-700'
                            }`}
                        >
                          {active && <Check className="h-3 w-3" />}
                          <span>{g.genre}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              <button
                type="submit"
                disabled={loading}
                className="flex w-full items-center justify-center gap-2 rounded-xl bg-brand-600 py-3 text-sm font-semibold text-white shadow-sm hover:bg-brand-700 disabled:opacity-60 transition-colors"
              >
                {loading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : mode === 'login' ? (
                  <LogIn className="h-4 w-4" />
                ) : (
                  <UserPlus className="h-4 w-4" />
                )}
                <span>
                  {mode === 'login' ? 'Entrar a mi cuenta' : 'Crear cuenta y descubrir libros'}
                </span>
              </button>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}
