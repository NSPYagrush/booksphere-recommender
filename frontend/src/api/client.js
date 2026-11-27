const TOKEN_KEY = 'booksphere_session_token';

export function getStoredToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setStoredToken(token) {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

let unauthorizedHandler = null;
export function onUnauthorized(cb) {
  unauthorizedHandler = cb;
}

async function request(path, options = {}) {
  const token = getStoredToken();
  const headers = {
    ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.headers || {}),
  };

  const res = await fetch(`/api${path}`, {
    ...options,
    headers,
  });

  const data = await res.json().catch(() => null);

  if (!res.ok) {
    if (res.status === 401 && unauthorizedHandler && !path.startsWith('/auth/login')) {
      unauthorizedHandler();
    }
    const detail = data?.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
        ? detail.map((d) => d.msg).join(', ')
        : `Error HTTP ${res.status}`;
    throw new Error(message);
  }

  return data;
}

export const api = {
  // Autenticación
  register: (payload) =>
    request('/auth/register', { method: 'POST', body: JSON.stringify(payload) }),
  login: (payload) =>
    request('/auth/login', { method: 'POST', body: JSON.stringify(payload) }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  getMe: () => request('/auth/me'),
  updateFavoriteGenres: (favorite_genres) =>
    request('/auth/favorite-genres', {
      method: 'PUT',
      body: JSON.stringify({ favorite_genres }),
    }),

  // Buscador y catálogo
  getGenres: () => request('/books/genres'),
  searchBooks: (params = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') {
        qs.set(k, String(v));
      }
    });
    return request(`/books/search?${qs.toString()}`);
  },
  getBookDetail: (bookId) => request(`/books/${bookId}`),

  // Lista de lectura dinámica
  getReadingList: (status = '') =>
    request(`/reading-list${status ? `?status=${encodeURIComponent(status)}` : ''}`),
  upsertReadingList: (bookId, status, rating = null) =>
    request(`/reading-list/${bookId}`, {
      method: 'PUT',
      body: JSON.stringify({ status, rating }),
    }),
  removeFromReadingList: (bookId) =>
    request(`/reading-list/${bookId}`, { method: 'DELETE' }),

  // Recomendador híbrido (3 ubicaciones)
  getHomeRecommendations: (topN = 12) =>
    request(`/recommendations/home?top_n=${topN}`),
  getGenreRecommendations: (genre, topN = 10) =>
    request(`/recommendations/genre/${encodeURIComponent(genre)}?top_n=${topN}`),
  getSimilarBooks: (bookId, topN = 8, minOverlap = 5) =>
    request(`/recommendations/similar/${bookId}?top_n=${topN}&min_overlap=${minOverlap}`),

  // Comunidad
  getNeighborhood: (limit = 12) =>
    request(`/community/neighborhood?limit=${limit}`),
  getUserTopBooks: (targetUserId) =>
    request(`/community/users/${targetUserId}/top-books`),
  getCommunityFavorites: (limit = 10) =>
    request(`/community/favorites?limit=${limit}`),

  // Evaluación offline
  getEvaluation: (recompute = false) =>
    request(`/evaluation${recompute ? '?recompute=true' : ''}`),
};
