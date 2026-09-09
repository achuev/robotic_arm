import type { AppConfig, SessionResponse, StatusResponse } from './protocol';

/**
 * REST-клиент шлюза. Пути — из docs/api.md §2, менять нельзя.
 */

const API_BASE = (import.meta.env?.VITE_API_BASE as string | undefined) ?? '';

export const TOKEN_STORAGE_KEY = 'so101_token';

function url(path: string): string {
  return `${API_BASE}${path}`;
}

/** Тот же адрес, но наружу: админский клиент ходит мимо `getJson`. */
export function apiUrl(path: string): string {
  return url(path);
}

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url(path), {
    credentials: 'include', // токен дублируется в httpOnly-cookie
    ...init,
  });
  if (!res.ok) {
    throw new ApiError(res.status, `${path} → ${res.status}`);
  }
  return (await res.json()) as T;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

/** `GET /api/status` — публичный, без авторизации. Для лендинга. */
export function fetchStatus(signal?: AbortSignal): Promise<StatusResponse> {
  return getJson<StatusResponse>('/api/status', { signal });
}

/** `GET /api/config` — читается один раз при загрузке. */
export function fetchConfig(signal?: AbortSignal): Promise<AppConfig> {
  return getJson<AppConfig>('/api/config', { signal });
}

/** `POST /api/session` — идемпотентен, вернёт тот же токен, если он уже в cookie. */
export function createSession(signal?: AbortSignal): Promise<SessionResponse> {
  return getJson<SessionResponse>('/api/session', { method: 'POST', signal });
}

/* --------------------------------------------------------------- токен */

export function readStoredToken(): string | null {
  try {
    const v = localStorage.getItem(TOKEN_STORAGE_KEY);
    return v && v.length > 0 ? v : null;
  } catch {
    // Приватный режим Safari умеет швырять на localStorage.
    return null;
  }
}

export function storeToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    /* не смертельно: cookie на сервере всё равно есть */
  }
}

/**
 * Токен для WS: сначала пробуем сохранённый (он же вернёт ход после обрыва),
 * иначе просим новый у шлюза.
 */
export async function ensureToken(signal?: AbortSignal): Promise<string> {
  const stored = readStoredToken();
  if (stored) return stored;
  const { token } = await createSession(signal);
  storeToken(token);
  return token;
}

/* ------------------------------------------- токен служебного наблюдателя */

/**
 * Отдельный токен для админской вкладки.
 *
 * Админка подключается к тому же `/ws`, но НЕ должна брать токен посетителя:
 * контракт §4 велит вытеснять прежнее соединение с тем же токеном, и панель,
 * открытая рядом с `/control` на одном ноутбуке, выбивала бы вкладку
 * управления (и наоборот) по кругу. `POST /api/session` тут не помогает —
 * он идемпотентен по cookie и вернёт ровно тот же токен.
 *
 * Поэтому админка генерирует свой UUIDv4 и живёт наблюдателем: наблюдателей
 * может быть сколько угодно, в `max_queue` они не считаются (контракт §4).
 */
export const ADMIN_WS_TOKEN_KEY = 'so101_admin_ws_token';

function randomUuid(): string {
  const c = globalThis.crypto as { randomUUID?: () => string } | undefined;
  if (c?.randomUUID) return c.randomUUID();
  // Старый Safari без randomUUID: собираем вручную, форма важнее энтропии.
  const hex = '0123456789abcdef';
  let out = '';
  for (let i = 0; i < 36; i++) {
    if (i === 8 || i === 13 || i === 18 || i === 23) out += '-';
    else if (i === 14) out += '4';
    else if (i === 19) out += hex[((Math.random() * 4) | 0) + 8];
    else out += hex[(Math.random() * 16) | 0];
  }
  return out;
}

export function ensureAdminObserverToken(): string {
  try {
    const stored = localStorage.getItem(ADMIN_WS_TOKEN_KEY);
    if (stored) return stored;
    const fresh = randomUuid();
    localStorage.setItem(ADMIN_WS_TOKEN_KEY, fresh);
    return fresh;
  } catch {
    // Приватный режим: токен проживёт до перезагрузки — панели этого хватит.
    return randomUuid();
  }
}

/** Адрес WebSocket. Пусто в конфиге — тот же origin, путь `/ws`. */
export function websocketUrl(): string {
  const explicit = import.meta.env?.VITE_WS_URL as string | undefined;
  if (explicit) return explicit;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}

/** URL для QR-кода на лендинге: берётся из сборки, не из хоста. */
export function publicControlUrl(): string {
  const configured = import.meta.env?.VITE_PUBLIC_URL as string | undefined;
  if (configured) return configured;
  return `${location.origin}/control`;
}

/** Абсолютный адрес MJPEG-потока из `config.video_url`. */
export function videoUrl(config: Pick<AppConfig, 'video_url'>): string {
  const v = config.video_url;
  if (/^https?:\/\//i.test(v)) return v;
  return `${API_BASE}${v}`;
}
