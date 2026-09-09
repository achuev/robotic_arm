import { apiUrl } from './api';
import { systemClock, type Clock, type TimerHandle } from './clock';
import type { RobotStatus } from './protocol';

/**
 * Логика админской панели: транспорт к `/api/admin/*`, опрос очереди и
 * подтверждения опасных действий.
 *
 * Вся она живёт отдельно от React ровно по той же причине, что и
 * `ConnectionManager`: у стенда стоит живой человек, и поведение кнопки
 * «СТОП» должно проверяться тестом, а не глазами на демо.
 *
 * Контракт: docs/api.md §2, «Админские». Токен уходит ТОЛЬКО заголовком
 * `X-Admin-Token` — ни в URL, ни в теле, иначе он осядет в логах прокси
 * и в истории браузера.
 */

/* ------------------------------------------------------------------ ошибки */

/** `403`: шлюз не принял токен. Отдельный тип — на него другая реакция. */
export class AdminAuthError extends Error {
  constructor(message = 'Шлюз не принял админский токен') {
    super(message);
    this.name = 'AdminAuthError';
  }
}

/** Сеть легла, шлюз ответил 5xx, ответ не разобрался. */
export class AdminNetworkError extends Error {
  constructor(message = 'Нет связи со шлюзом') {
    super(message);
    this.name = 'AdminNetworkError';
  }
}

/* ------------------------------------------------------------------ снимок */

export interface AdminControllerInfo {
  token: string;
  connected: boolean;
  /** UNIX-время в секундах; null — шлюз не прислал. */
  expiresAt: number | null;
  remainingSec: number | null;
  idleForSec: number | null;
  /** Сколько секунд оператор в обрыве (идёт RECONNECT_GRACE). */
  disconnectedForSec: number | null;
}

export interface AdminQueueEntry {
  token: string;
  position: number;
  etaSec: number | null;
  connected: boolean;
}

export interface AdminCooldown {
  token: string;
  secondsLeft: number;
}

export interface AdminSnapshot {
  /** Время сервера, UNIX-секунды. Null — шлюз не прислал (тогда часы свои). */
  serverNow: number | null;
  robot: RobotStatus;
  estop: boolean;
  /** До какого момента идёт передача хода (рука уезжает в home). */
  handoverUntil: number | null;
  controller: AdminControllerInfo | null;
  queue: AdminQueueEntry[];
  observers: string[];
  cooldowns: AdminCooldown[];
}

export const EMPTY_SNAPSHOT: AdminSnapshot = {
  serverNow: null,
  robot: 'idle',
  estop: false,
  handoverUntil: null,
  controller: null,
  queue: [],
  observers: [],
  cooldowns: [],
};

const ROBOT_STATUSES: readonly string[] = ['idle', 'moving', 'error', 'estop'];

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function bool(v: unknown, fallback = false): boolean {
  return typeof v === 'boolean' ? v : fallback;
}

/**
 * Разбор `GET /api/admin/queue`.
 *
 * Терпимый: формы тела в docs/api.md нет (см. отчёт), поэтому берём то, что
 * есть, и не падаем на пропусках. Отдельно поддержан вариант, где
 * `controller` — просто строка с токеном: так отвечали ранние сборки мока.
 */
export function parseAdminSnapshot(raw: unknown): AdminSnapshot {
  if (typeof raw !== 'object' || raw === null) return { ...EMPTY_SNAPSHOT };
  const d = raw as Record<string, unknown>;

  const robotRaw = str(d.robot);
  const estop = bool(d.estop);
  const robot: RobotStatus = ROBOT_STATUSES.includes(robotRaw)
    ? (robotRaw as RobotStatus)
    : estop
      ? 'estop'
      : 'idle';

  let controller: AdminControllerInfo | null = null;
  const c = d.controller;
  if (typeof c === 'string' && c) {
    controller = {
      token: c,
      connected: true,
      expiresAt: num(d.expires_at),
      remainingSec: null,
      idleForSec: null,
      disconnectedForSec: null,
    };
  } else if (typeof c === 'object' && c !== null) {
    const co = c as Record<string, unknown>;
    controller = {
      token: str(co.token),
      connected: bool(co.connected, true),
      expiresAt: num(co.expires_at),
      remainingSec: num(co.remaining_sec),
      idleForSec: num(co.idle_for_sec),
      disconnectedForSec: num(co.disconnected_for_sec),
    };
  }

  const queue: AdminQueueEntry[] = Array.isArray(d.queue)
    ? d.queue.flatMap((item, i) => {
        if (typeof item === 'string') {
          return [{ token: item, position: i + 1, etaSec: null, connected: true }];
        }
        if (typeof item !== 'object' || item === null) return [];
        const q = item as Record<string, unknown>;
        return [
          {
            token: str(q.token),
            position: num(q.position) ?? i + 1,
            etaSec: num(q.eta_sec),
            connected: bool(q.connected, true),
          },
        ];
      })
    : [];

  const observers: string[] = Array.isArray(d.observers)
    ? d.observers.filter((o): o is string => typeof o === 'string')
    : [];

  const cooldowns: AdminCooldown[] =
    typeof d.cooldowns === 'object' && d.cooldowns !== null
      ? Object.entries(d.cooldowns as Record<string, unknown>).flatMap(
          ([token, left]) => {
            const secondsLeft = num(left);
            return secondsLeft === null ? [] : [{ token, secondsLeft }];
          }
        )
      : [];

  return {
    serverNow: num(d.now),
    robot,
    estop,
    handoverUntil: num(d.handover_until),
    controller,
    queue,
    observers,
    cooldowns,
  };
}

/* ------------------------------------------------------------------ транспорт */

export interface AdminFetchResponse {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
}

export interface AdminFetchInit {
  method: string;
  headers: Record<string, string>;
  signal?: AbortSignal;
}

export type AdminFetch = (
  url: string,
  init: AdminFetchInit
) => Promise<AdminFetchResponse>;

const browserFetch: AdminFetch = (url, init) =>
  fetch(url, {
    method: init.method,
    headers: init.headers,
    signal: init.signal,
    // Админка не полагается на cookie-сессию посетителя: авторизует заголовок.
    cache: 'no-store',
  });

export type AdminAction = 'kick' | 'estop' | 'release_estop';

const ACTION_PATH: Record<AdminAction, string> = {
  kick: '/api/admin/kick',
  estop: '/api/admin/estop',
  release_estop: '/api/admin/release_estop',
};

/** Что делает кнопка — для подписи в подтверждении и в уведомлении. */
export const ACTION_TITLE: Record<AdminAction, string> = {
  kick: 'Снять оператора',
  estop: 'Аварийная остановка',
  release_estop: 'Снять аварийную остановку',
};

export const ACTION_DONE: Record<AdminAction, string> = {
  kick: 'Оператор снят, ход ушёл следующему',
  estop: 'Робот остановлен',
  release_estop: 'Аварийная остановка снята, рука снова подвижна',
};

/**
 * Какие действия требуют подтверждения.
 *
 * «СТОП» — потому что случайный клик обрывает чужой ход и замораживает стенд;
 * «снять стоп» — потому что она возвращает руке подвижность, и человек
 * должен успеть убрать пальцы. «Снять оператора» подтверждения не требует:
 * это штатный жест, ради которого панель и открывают, и лишний клик тут
 * только мешает.
 */
export const NEEDS_CONFIRM: readonly AdminAction[] = ['estop', 'release_estop'];

export function needsConfirm(action: AdminAction): boolean {
  return NEEDS_CONFIRM.includes(action);
}

/**
 * Потолок ожидания ответа.
 *
 * Не роскошь: чёрная дыра в сети (Wi-Fi стенда пропал, но сокет не закрылся)
 * подвешивает `fetch` без ошибки. Без таймаута кнопка «СТОП» осталась бы
 * заблокированной «выполняется…» навсегда — ровно тогда, когда она нужна.
 */
export const DEFAULT_REQUEST_TIMEOUT_MS = 4000;

export class AdminApi {
  private readonly getToken: () => string;
  private readonly fetchImpl: AdminFetch;
  private readonly clock: Clock;
  private readonly timeoutMs: number;

  constructor(opts: {
    getToken: () => string;
    fetchImpl?: AdminFetch;
    clock?: Clock;
    timeoutMs?: number;
  }) {
    this.getToken = opts.getToken;
    this.fetchImpl = opts.fetchImpl ?? browserFetch;
    this.clock = opts.clock ?? systemClock;
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
  }

  private async call(path: string, method: string): Promise<unknown> {
    const token = this.getToken();
    // Без токена запрос не отправляем вовсе: 403 в логах шлюза бесполезен.
    if (!token) throw new AdminAuthError('Введите админский токен');

    let res: AdminFetchResponse;
    try {
      res = await this.withTimeout((signal) =>
        this.fetchImpl(apiUrl(path), {
          method,
          headers: { 'X-Admin-Token': token },
          signal,
        })
      );
    } catch (err) {
      if (err instanceof AdminNetworkError) throw err;
      throw new AdminNetworkError(
        err instanceof Error ? err.message : 'Нет связи со шлюзом'
      );
    }

    // Контракт: без валидного токена 403 и никаких подробностей.
    if (res.status === 403) throw new AdminAuthError();
    if (!res.ok) throw new AdminNetworkError(`${path} → ${res.status}`);

    try {
      return await res.json();
    } catch {
      // Тело не обязано быть JSON-ом (у kick/estop оно нам и не нужно).
      return {};
    }
  }

  /** Гонка запроса с таймером: кто первый, тот и ответ. */
  private withTimeout(
    run: (signal?: AbortSignal) => Promise<AdminFetchResponse>
  ): Promise<AdminFetchResponse> {
    const controller =
      typeof AbortController === 'function' ? new AbortController() : null;
    let handle: TimerHandle | null = null;

    const timeout = new Promise<never>((_, reject) => {
      handle = this.clock.setTimeout(() => {
        handle = null;
        controller?.abort();
        reject(new AdminNetworkError('Шлюз не ответил вовремя'));
      }, this.timeoutMs);
    });

    return Promise.race([run(controller?.signal), timeout]).finally(() => {
      if (handle !== null) this.clock.clearTimeout(handle);
    });
  }

  async queue(): Promise<AdminSnapshot> {
    return parseAdminSnapshot(await this.call('/api/admin/queue', 'GET'));
  }

  async perform(action: AdminAction): Promise<void> {
    await this.call(ACTION_PATH[action], 'POST');
  }
}

/* ------------------------------------------------------------------ хранение */

/**
 * Токен запоминается в localStorage. Это не пароль уровня банка, но и в URL
 * его класть нельзя: адрес попадает в историю, в Referer и в логи прокси.
 */
export const ADMIN_TOKEN_STORAGE_KEY = 'so101_admin_token';

export interface TokenStorage {
  read(): string;
  write(value: string): void;
  clear(): void;
}

export const localTokenStorage: TokenStorage = {
  read() {
    try {
      return localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) ?? '';
    } catch {
      // Приватный режим Safari умеет швырять на localStorage.
      return '';
    }
  },
  write(value) {
    try {
      localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, value);
    } catch {
      /* переживём: токен останется в памяти до перезагрузки */
    }
  },
  clear() {
    try {
      localStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
    } catch {
      /* нечего чистить */
    }
  },
};

export function memoryTokenStorage(initial = ''): TokenStorage {
  let value = initial;
  return {
    read: () => value,
    write: (v) => {
      value = v;
    },
    clear: () => {
      value = '';
    },
  };
}

/* ------------------------------------------------------------------ состояние */

export type AdminLink =
  /** Токена нет — ждём, пока его введут. */
  | 'idle'
  /** Запрос в полёте, ответа ещё не было. */
  | 'loading'
  /** Последний опрос удался. */
  | 'ok'
  /** Шлюз ответил 403. */
  | 'forbidden'
  /** Шлюз не отвечает. */
  | 'offline';

export interface AdminNotice {
  id: number;
  kind: 'ok' | 'error';
  text: string;
}

export interface AdminState {
  /** Что набрано/сохранено; в запрос уходит именно это. */
  token: string;
  /** Хотя бы один успешный ответ с этим токеном. */
  authorized: boolean;
  link: AdminLink;
  snapshot: AdminSnapshot | null;
  /** Локальное время получения снимка, мс. */
  snapshotAt: number | null;
  /** `serverNow - localNow`, секунды: ноутбук и шлюз редко идут секунда в секунду. */
  clockOffsetSec: number;
  /** Действие, ждущее подтверждения. */
  pendingConfirm: AdminAction | null;
  /** Действие, выполняющееся прямо сейчас. */
  busy: AdminAction | null;
  notice: AdminNotice | null;
  /** Сколько опросов подряд не удалось — по нему решаем, врать ли цифрами. */
  failures: number;
}

export const initialAdminState: AdminState = {
  token: '',
  authorized: false,
  link: 'idle',
  snapshot: null,
  snapshotAt: null,
  clockOffsetSec: 0,
  pendingConfirm: null,
  busy: null,
  notice: null,
  failures: 0,
};

export const DEFAULT_POLL_INTERVAL_MS = 1000;
/**
 * Сколько подтверждение остаётся «взведённым». Достаточно, чтобы дотянуться
 * до второй кнопки, и мало, чтобы случайно нажатый час назад «СТОП» не ждал
 * подтверждения до вечера.
 */
export const DEFAULT_CONFIRM_TIMEOUT_MS = 10_000;
/** Снимок старше этого — рисуем приглушённо: цифрам верить уже нельзя. */
export const STALE_SNAPSHOT_MS = 4000;

export interface AdminSessionOptions {
  clock?: Clock;
  fetchImpl?: AdminFetch;
  api?: AdminApi;
  storage?: TokenStorage;
  pollIntervalMs?: number;
  confirmTimeoutMs?: number;
  /** Потолок ожидания ответа шлюза. */
  requestTimeoutMs?: number;
}

let noticeSeq = 0;

/**
 * Сессия админки: держит токен, опрашивает очередь и выполняет действия.
 * Ничего не знает про React — наружу торчат `getState`/`subscribe`.
 */
export class AdminSession {
  private readonly clock: Clock;
  private readonly storage: TokenStorage;
  private readonly api: AdminApi;
  private readonly pollIntervalMs: number;
  private readonly confirmTimeoutMs: number;

  private state: AdminState;
  private listeners = new Set<(s: AdminState) => void>();
  private pollTimer: TimerHandle | null = null;
  private confirmTimer: TimerHandle | null = null;
  private running = false;
  /** Номер поколения запроса: ответ от старого токена игнорируем. */
  private generation = 0;

  /** Диагностика для тестов. */
  pollCount = 0;

  constructor(options: AdminSessionOptions = {}) {
    this.clock = options.clock ?? systemClock;
    this.storage = options.storage ?? localTokenStorage;
    this.pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
    this.confirmTimeoutMs = options.confirmTimeoutMs ?? DEFAULT_CONFIRM_TIMEOUT_MS;
    this.api =
      options.api ??
      new AdminApi({
        getToken: () => this.state.token,
        fetchImpl: options.fetchImpl,
        clock: this.clock,
        timeoutMs: options.requestTimeoutMs,
      });
    this.state = { ...initialAdminState, token: this.storage.read() };
  }

  /* ------------------------------------------------------------ подписка */

  getState(): AdminState {
    return this.state;
  }

  subscribe(fn: (s: AdminState) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private patch(next: Partial<AdminState>): void {
    this.state = { ...this.state, ...next };
    for (const fn of this.listeners) fn(this.state);
  }

  private notify(kind: 'ok' | 'error', text: string): void {
    this.patch({ notice: { id: ++noticeSeq, kind, text } });
  }

  dismissNotice(): void {
    if (this.state.notice) this.patch({ notice: null });
  }

  /* ------------------------------------------------------------ токен */

  /** Токен из поля ввода. Сохраняем его только после удачного ответа. */
  setToken(token: string): void {
    const value = token.trim();
    if (value === this.state.token) return;
    this.generation++;
    this.patch({
      token: value,
      authorized: false,
      link: 'idle',
      failures: 0,
      notice: null,
    });
  }

  /** Забыть токен и всё, что он показывал. */
  signOut(): void {
    this.generation++;
    this.stopPolling();
    this.cancelConfirm();
    this.storage.clear();
    this.patch({
      token: '',
      authorized: false,
      link: 'idle',
      snapshot: null,
      snapshotAt: null,
      failures: 0,
      notice: null,
    });
  }

  /* ------------------------------------------------------------ опрос */

  start(): void {
    if (this.running) return;
    this.running = true;
    if (!this.state.token) {
      this.patch({ link: 'idle' });
      return;
    }
    void this.poll();
  }

  stop(): void {
    this.running = false;
    this.stopPolling();
    this.clearConfirmTimer();
  }

  /** Перезапустить опрос после ввода нового токена. */
  retry(): void {
    if (!this.state.token) {
      this.patch({ link: 'idle' });
      return;
    }
    this.running = true;
    this.stopPolling();
    void this.poll();
  }

  private stopPolling(): void {
    if (this.pollTimer !== null) {
      this.clock.clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
  }

  private schedulePoll(): void {
    if (!this.running || this.pollTimer !== null) return;
    this.pollTimer = this.clock.setTimeout(() => {
      this.pollTimer = null;
      void this.poll();
    }, this.pollIntervalMs);
  }

  /** Один опрос очереди. Возвращает true, если снимок обновился. */
  async poll(): Promise<boolean> {
    if (!this.state.token) {
      this.patch({ link: 'idle' });
      return false;
    }
    const gen = this.generation;
    this.pollCount++;
    if (this.state.snapshot === null) this.patch({ link: 'loading' });

    try {
      const snapshot = await this.api.queue();
      if (gen !== this.generation) return false;
      const nowMs = this.clock.now();
      this.storage.write(this.state.token);
      this.patch({
        snapshot,
        snapshotAt: nowMs,
        clockOffsetSec:
          snapshot.serverNow === null ? 0 : snapshot.serverNow - nowMs / 1000,
        authorized: true,
        link: 'ok',
        failures: 0,
      });
      this.schedulePoll();
      return true;
    } catch (err) {
      if (gen !== this.generation) return false;
      this.handleFailure(err);
      return false;
    }
  }

  private handleFailure(err: unknown): void {
    if (err instanceof AdminAuthError) {
      // Токен неверен: опрос прекращаем — долбить шлюз 403-ми незачем,
      // а сохранённое значение стираем, чтобы оно не всплыло после F5.
      this.stopPolling();
      this.cancelConfirm();
      this.storage.clear();
      this.patch({
        authorized: false,
        link: 'forbidden',
        snapshot: null,
        snapshotAt: null,
        notice: { id: ++noticeSeq, kind: 'error', text: err.message },
      });
      return;
    }
    // Связь. Последний снимок оставляем — он лучше пустого экрана,
    // а его возраст панель показывает отдельно.
    this.patch({ link: 'offline', failures: this.state.failures + 1 });
    this.schedulePoll();
  }

  /* ------------------------------------------------------------ действия */

  /**
   * Нажатие на кнопку. Опасные действия сначала взводят подтверждение и
   * НИЧЕГО не отправляют — это и есть защита от случайного клика.
   */
  request(action: AdminAction): void {
    if (this.state.busy) return;
    if (needsConfirm(action)) {
      this.armConfirm(action);
      return;
    }
    void this.perform(action);
  }

  /** Второе нажатие: выполнить взведённое действие. */
  confirm(): void {
    const action = this.state.pendingConfirm;
    if (!action) return;
    this.clearConfirmTimer();
    this.patch({ pendingConfirm: null });
    void this.perform(action);
  }

  cancelConfirm(): void {
    this.clearConfirmTimer();
    if (this.state.pendingConfirm) this.patch({ pendingConfirm: null });
  }

  private armConfirm(action: AdminAction): void {
    this.clearConfirmTimer();
    this.patch({ pendingConfirm: action, notice: null });
    this.confirmTimer = this.clock.setTimeout(() => {
      this.confirmTimer = null;
      // Взведённая кнопка не должна ждать вечно: человек мог отойти.
      if (this.state.pendingConfirm) this.patch({ pendingConfirm: null });
    }, this.confirmTimeoutMs);
  }

  private clearConfirmTimer(): void {
    if (this.confirmTimer !== null) {
      this.clock.clearTimeout(this.confirmTimer);
      this.confirmTimer = null;
    }
  }

  /** Выполнить действие без подтверждения. Публично — для тестов и kick. */
  async perform(action: AdminAction): Promise<boolean> {
    if (this.state.busy) return false;
    const gen = this.generation;
    this.patch({ busy: action, notice: null });
    try {
      await this.api.perform(action);
      if (gen !== this.generation) return false;
      this.patch({ busy: null });
      this.notify('ok', ACTION_DONE[action]);
      // Не ждём следующего тика: человек должен увидеть результат сразу.
      this.stopPolling();
      await this.poll();
      return true;
    } catch (err) {
      if (gen !== this.generation) return false;
      this.patch({ busy: null });
      if (err instanceof AdminAuthError) {
        this.handleFailure(err);
      } else {
        this.notify(
          'error',
          `${ACTION_TITLE[action]}: не прошло. ${
            err instanceof Error ? err.message : ''
          }`.trim()
        );
        this.patch({ link: 'offline', failures: this.state.failures + 1 });
        this.schedulePoll();
      }
      return false;
    }
  }
}

/* ------------------------------------------------------------------ селекторы */

/**
 * Сколько осталось у текущего оператора.
 *
 * `expires_at` — абсолютное время СЕРВЕРА, поэтому вычитаем не локальные
 * часы, а поправленные на измеренное расхождение. Иначе ноутбук, у которого
 * время убежало на минуту, покажет «-60» или «150» вместо честных 90.
 */
export function controllerRemainingSec(state: AdminState, nowMs: number): number {
  const c = state.snapshot?.controller;
  if (!c) return 0;
  if (c.expiresAt !== null) {
    return Math.max(0, Math.ceil(c.expiresAt - (nowMs / 1000 + state.clockOffsetSec)));
  }
  if (c.remainingSec !== null && state.snapshotAt !== null) {
    const elapsed = (nowMs - state.snapshotAt) / 1000;
    return Math.max(0, Math.ceil(c.remainingSec - elapsed));
  }
  return 0;
}

/** Сколько оператор уже бездействует (IDLE_TIMEOUT тикает от этого). */
export function controllerIdleSec(state: AdminState, nowMs: number): number {
  const c = state.snapshot?.controller;
  if (!c || c.idleForSec === null || state.snapshotAt === null) return 0;
  return Math.max(0, c.idleForSec + (nowMs - state.snapshotAt) / 1000);
}

export function snapshotAgeMs(state: AdminState, nowMs: number): number {
  if (state.snapshotAt === null) return Infinity;
  return Math.max(0, nowMs - state.snapshotAt);
}

/** Данные устарели: связь потеряна и последний снимок уже не про «сейчас». */
export function isSnapshotStale(
  state: AdminState,
  nowMs: number,
  thresholdMs = STALE_SNAPSHOT_MS
): boolean {
  if (state.snapshot === null) return false;
  return snapshotAgeMs(state, nowMs) > thresholdMs;
}

/** Число участников: оператор плюс ждущие — именно это ограничивает max_queue. */
export function participants(snapshot: AdminSnapshot | null): number {
  if (!snapshot) return 0;
  return snapshot.queue.length + (snapshot.controller ? 1 : 0);
}

/** Короткий вид токена для таблицы: полный никому не нужен и мешает читать. */
export function shortToken(token: string): string {
  return token.length <= 8 ? token : `${token.slice(0, 8)}…`;
}
