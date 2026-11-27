import React from 'react';
import {
  BookOpen,
  Compass,
  Library,
  Users,
  BarChart3,
  LogOut,
  Sparkles,
  Snowflake,
} from 'lucide-react';

const NAV_ITEMS = [
  { id: 'home', label: 'Inicio y Buscador', icon: BookOpen },
  { id: 'genres', label: 'Géneros', icon: Compass },
  { id: 'mylist', label: 'Mi Lista', icon: Library },
  { id: 'community', label: 'Comunidad', icon: Users },
  { id: 'metrics', label: 'Evaluación', icon: BarChart3 },
];

export default function Navbar({ user, activeTab, onTabChange, onLogout }) {
  const totalBooks = user?.stats?.total_books ?? 0;
  const isColdStart = user?.stats?.recommender_mode === 'cold_start';

  return (
    <header className="sticky top-0 z-30 border-b border-stone-200/80 bg-white/95 backdrop-blur-md">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="flex h-16 items-center justify-between gap-4">
          {/* Marca */}
          <button
            onClick={() => onTabChange('home')}
            className="flex items-center gap-2.5 text-left group focus:outline-none"
          >
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand-600 text-white shadow-sm group-hover:bg-brand-700 transition-colors">
              <BookOpen className="h-5 w-5" />
            </div>
            <div className="leading-tight">
              <span className="font-editorial text-xl font-bold tracking-tight text-stone-900">
                BookSphere
              </span>
            </div>
          </button>

          {/* Navegación desktop */}
          <nav className="hidden md:flex items-center gap-1">
            {NAV_ITEMS.map(({ id, label, icon: Icon }) => {
              const active = activeTab === id;
              return (
                <button
                  key={id}
                  onClick={() => onTabChange(id)}
                  className={`flex items-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium transition-all ${active
                    ? 'bg-brand-50 text-brand-700 shadow-sm ring-1 ring-brand-200/70'
                    : 'text-stone-600 hover:bg-stone-100 hover:text-stone-900'
                    }`}
                >
                  <Icon className="h-4 w-4" />
                  <span>{label}</span>
                  {id === 'mylist' && (
                    <span
                      className={`ml-0.5 rounded-full px-2 py-0.5 text-xs font-semibold ${active
                        ? 'bg-brand-600 text-white'
                        : 'bg-stone-200 text-stone-700'
                        }`}
                    >
                      {totalBooks}
                    </span>
                  )}
                </button>
              );
            })}
          </nav>

          {/* Perfil + Logoff */}
          <div className="flex items-center gap-3">
            <button
              onClick={onLogout}
              title="Cerrar sesión (Logoff)"
              className="inline-flex items-center gap-1.5 rounded-lg border border-stone-200 bg-white px-3 py-2 text-xs font-semibold text-stone-700 hover:border-rose-200 hover:bg-rose-50 hover:text-rose-700 transition-colors"
            >
              <LogOut className="h-4 w-4" />
              <span className="hidden sm:inline">Cerrar sesión</span>
            </button>
          </div>
        </div>

        {/* Navegación móvil */}
        <div className="flex md:hidden items-center gap-1 overflow-x-auto pb-2.5 pt-1">
          {NAV_ITEMS.map(({ id, label, icon: Icon }) => {
            const active = activeTab === id;
            return (
              <button
                key={id}
                onClick={() => onTabChange(id)}
                className={`flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium ${active
                  ? 'bg-brand-600 text-white'
                  : 'bg-stone-100 text-stone-600'
                  }`}
              >
                <Icon className="h-3.5 w-3.5" />
                <span>{label}</span>
                {id === 'mylist' && <span>({totalBooks})</span>}
              </button>
            );
          })}
        </div>
      </div>
    </header>
  );
}
