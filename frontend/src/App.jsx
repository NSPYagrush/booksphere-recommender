import React, { useCallback, useEffect, useState } from 'react';
import { Loader2, CheckCircle2, AlertCircle } from 'lucide-react';
import { api, getStoredToken, onUnauthorized, setStoredToken } from './api/client';
import Navbar from './components/Navbar';
import BookModal from './components/BookModal';
import AuthPage from './pages/AuthPage';
import HomePage from './pages/HomePage';
import GenresPage from './pages/GenresPage';
import MyListPage from './pages/MyListPage';
import CommunityPage from './pages/CommunityPage';
import MetricsPage from './pages/MetricsPage';

export default function App() {
  const [user, setUser] = useState(null);
  const [booting, setBooting] = useState(true);
  const [activeTab, setActiveTab] = useState('home');
  const [genres, setGenres] = useState([]);
  const [readingMap, setReadingMap] = useState({});
  const [listVersion, setListVersion] = useState(0);
  const [modalBookId, setModalBookId] = useState(null);
  const [toast, setToast] = useState(null);

  const showToast = useCallback((message, type = 'success') => {
    setToast({ message, type });
    setTimeout(() => {
      setToast((prev) => (prev?.message === message ? null : prev));
    }, 3200);
  }, []);

  const clearSession = useCallback(() => {
    setStoredToken(null);
    setUser(null);
    setReadingMap({});
    setModalBookId(null);
    setActiveTab('home');
  }, []);

  useEffect(() => {
    onUnauthorized(clearSession);
  }, [clearSession]);

  const hydrateUserData = useCallback(async () => {
    const [meRes, genresRes, listRes] = await Promise.all([
      api.getMe(),
      api.getGenres(),
      api.getReadingList(),
    ]);
    setUser(meRes.user);
    setGenres(genresRes.genres || []);
    const map = {};
    (listRes.items || []).forEach((it) => {
      map[it.book_id] = {
        status: it.status,
        rating: it.rating,
        effective_rating: it.effective_rating,
        is_implicit: it.is_implicit_rating,
      };
    });
    setReadingMap(map);
  }, []);

  // Restaurar sesión si existe token guardado
  useEffect(() => {
    const token = getStoredToken();
    if (!token) {
      setBooting(false);
      return;
    }
    hydrateUserData()
      .catch(() => {
        clearSession();
      })
      .finally(() => {
        setBooting(false);
      });
  }, [hydrateUserData, clearSession]);

  const handleAuthenticated = async (token, loggedUser) => {
    setStoredToken(token);
    setUser(loggedUser);
    setActiveTab('home');
    try {
      await hydrateUserData();
      setListVersion((v) => v + 1);
      showToast(`Bienvenido, ${loggedUser.display_name || loggedUser.username}`);
    } catch (err) {
      showToast(err.message, 'error');
    }
  };

  const handleLogout = async () => {
    try {
      await api.logout();
    } catch {
      // Ignorar si ya expiró
    }
    clearSession();
    showToast('Sesión cerrada correctamente.');
  };

  // Upsert en lista dinámica + refresco inmediato del recomendador
  const handleUpsertList = async (bookId, status, rating = null) => {
    try {
      const res = await api.upsertReadingList(bookId, status, rating);
      setReadingMap((prev) => ({
        ...prev,
        [bookId]: {
          status: res.status,
          rating: res.rating,
          effective_rating: res.effective_rating,
          is_implicit: res.is_implicit_rating,
        },
      }));
      const meRes = await api.getMe();
      setUser(meRes.user);
      setListVersion((v) => v + 1);

      const labelMap = { por_leer: 'Por leer', leyendo: 'Leyendo', leido: 'Leído' };
      showToast(
        rating != null
          ? `Calificado con ${rating}★ (${labelMap[status]}) · Recomendaciones actualizadas`
          : `Guardado como «${labelMap[status]}»`
      );
    } catch (err) {
      showToast(err.message, 'error');
    }
  };

  const handleRemoveList = async (bookId) => {
    try {
      await api.removeFromReadingList(bookId);
      setReadingMap((prev) => {
        const next = { ...prev };
        delete next[bookId];
        return next;
      });
      const meRes = await api.getMe();
      setUser(meRes.user);
      setListVersion((v) => v + 1);
      showToast('Libro eliminado de tu lista de lectura.');
    } catch (err) {
      showToast(err.message, 'error');
    }
  };

  const handleUpdateFavoriteGenres = async (nextGenres) => {
    try {
      const res = await api.updateFavoriteGenres(nextGenres);
      setUser(res.user);
      setListVersion((v) => v + 1);
      showToast('Géneros favoritos actualizados.');
    } catch (err) {
      showToast(err.message, 'error');
    }
  };

  if (booting) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-paper text-stone-500">
        <Loader2 className="h-8 w-8 animate-spin text-brand-600" />
        <p className="mt-3 text-sm font-medium">Cargando BookSphere...</p>
      </div>
    );
  }

  if (!user) {
    return (
      <>
        <AuthPage onAuthenticated={handleAuthenticated} />
        {toast && <ToastBanner toast={toast} />}
      </>
    );
  }

  return (
    <div className="min-h-screen bg-paper flex flex-col">
      <Navbar
        user={user}
        activeTab={activeTab}
        onTabChange={setActiveTab}
        onLogout={handleLogout}
      />

      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-8 sm:px-6 lg:px-8">
        {activeTab === 'home' && (
          <HomePage
            user={user}
            genres={genres}
            readingMap={readingMap}
            listVersion={listVersion}
            onSelectBook={setModalBookId}
            onUpsertList={handleUpsertList}
            onRemoveList={handleRemoveList}
          />
        )}

        {activeTab === 'genres' && (
          <GenresPage
            user={user}
            genres={genres}
            readingMap={readingMap}
            listVersion={listVersion}
            onSelectBook={setModalBookId}
            onUpsertList={handleUpsertList}
            onRemoveList={handleRemoveList}
          />
        )}

        {activeTab === 'mylist' && (
          <MyListPage
            user={user}
            genres={genres}
            readingMap={readingMap}
            listVersion={listVersion}
            onSelectBook={setModalBookId}
            onUpsertList={handleUpsertList}
            onRemoveList={handleRemoveList}
            onUpdateFavoriteGenres={handleUpdateFavoriteGenres}
          />
        )}

        {activeTab === 'community' && (
          <CommunityPage
            user={user}
            readingMap={readingMap}
            listVersion={listVersion}
            onSelectBook={setModalBookId}
            onUpsertList={handleUpsertList}
            onRemoveList={handleRemoveList}
          />
        )}

        {activeTab === 'metrics' && <MetricsPage />}
      </main>

      <footer className="border-t border-stone-200/80 bg-white py-5 text-center text-xs text-stone-500">
        BookSphere · Trabajo Final Web Social · Buscador + Recomendador Híbrido
      </footer>

      {modalBookId && (
        <BookModal
          bookId={modalBookId}
          readingMap={readingMap}
          onClose={() => setModalBookId(null)}
          onSelectBook={setModalBookId}
          onUpsertList={handleUpsertList}
          onRemoveList={handleRemoveList}
        />
      )}

      {toast && <ToastBanner toast={toast} />}
    </div>
  );
}

function ToastBanner({ toast }) {
  const isErr = toast.type === 'error';
  return (
    <div className="fixed bottom-5 right-5 z-50 max-w-sm">
      <div
        className={`flex items-center gap-2.5 rounded-2xl px-4 py-3 text-xs font-semibold shadow-lg ring-1 ${isErr
          ? 'bg-rose-600 text-white ring-rose-700'
          : 'bg-stone-900 text-white ring-stone-800'
          }`}
      >
        {isErr ? (
          <AlertCircle className="h-4 w-4 shrink-0 text-rose-200" />
        ) : (
          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-400" />
        )}
        <span>{toast.message}</span>
      </div>
    </div>
  );
}
