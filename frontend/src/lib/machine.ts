import {
  describeError,
  describeErrorMessage,
  describeRevoke,
  parseCooldownSeconds,
} from './errors';
import type { ErrorSeverity } from './errors';
import {
  CONFIG_DEFAULTS,
  type ErrorMsg,
  type RevokeReason,
  type RobotStatus,
  type ServerMessage,
} from './protocol';

/**
 * Конечный автомат клиента. Чистая функция: (состояние, событие, контекст) → состояние.
 * Ровно повторяет диаграмму из docs/api.md §1 плюс экран «время вышло»,
 * которого в контракте нет, но который нужен человеку.
 */

export type Phase =
  /** 1. Сокет ещё не отдал первое сообщение — короткий скелетон. */
  | 'connecting'
  /** 2. Смотрим, командовать не можем. */
  | 'observer'
  /** 3. Стоим в очереди, роботом управляет кто-то другой. */
  | 'queued'
  /** 4. Ход наш. */
  | 'controlling'
  /** 5. Ход только что закончился — объясняем, что дальше. */
  | 'expired'
  /**
   * 6. Эту вкладку вытеснило второе подключение с тем же токеном.
   * Ход и место в очереди не потеряны — они просто «переехали» в другую вкладку.
   */
  | 'displaced';

export interface Toast {
  id: number;
  /** Код ошибки, породившей тост: экран может решить не дублировать себя. */
  code: string;
  title: string;
  hint: string;
  severity: ErrorSeverity;
  ttlMs: number;
  createdAt: number;
}

export interface QueueInfo {
  position: number;
  ahead: number;
  etaSec: number;
  queueLength: number;
}

export interface ControlInfo {
  /**
   * Срок хода. `null` — ограничения нет: посетитель на стенде один, и
   * торопить его некого. Как только в очередь кто-то встаёт, сервер
   * присылает `granted` повторно, уже со сроком.
   */
  durationSec: number | null;
  /** UNIX-время в СЕКУНДАХ (как в контракте), либо null. */
  expiresAt: number | null;
}

export interface ClientState {
  phase: Phase;
  /** Сокет открыт прямо сейчас. */
  online: boolean;
  /** Был ли хоть один успешный коннект — чтобы отличать первый вход от обрыва. */
  everConnected: boolean;

  robot: RobotStatus;
  joints: Record<string, number>;
  ee: { x: number; y: number; z: number } | null;
  lastStateAt: number | null;

  queue: QueueInfo | null;
  control: ControlInfo | null;
  lastRevoke: RevokeReason | null;

  estop: boolean;
  /** Локальное время в мс, до которого нельзя вставать в очередь. */
  cooldownUntilMs: number | null;

  toast: Toast | null;
  /**
   * Орган, по которому прилетел `out_of_range`. Адрес приходит от сервера
   * в полях `joint`/`axis`, гадать по последней команде больше не нужно.
   */
  highlight: { kind: 'joint' | 'axis'; name: string } | null;
  /** Нажали «встать в очередь», ответа ещё нет. */
  pendingEnqueue: boolean;
  /**
   * Когда мы последний раз отправили `ping`. Сервер замораживает движение,
   * если не слышал нас дольше `watchdog_timeout_sec` — по этой метке экран
   * честно говорит, что рука встала (например, вкладка ушла в фон).
   */
  lastPingAt: number | null;
}

export const initialState: ClientState = {
  phase: 'connecting',
  online: false,
  everConnected: false,
  robot: 'idle',
  joints: {},
  ee: null,
  lastStateAt: null,
  queue: null,
  control: null,
  lastRevoke: null,
  estop: false,
  cooldownUntilMs: null,
  toast: null,
  highlight: null,
  pendingEnqueue: false,
  lastPingAt: null,
};

export type ClientEvent =
  | { type: 'socket_open' }
  | { type: 'socket_closed' }
  | { type: 'server'; msg: ServerMessage }
  | { type: 'enqueue_requested' }
  | { type: 'release_requested' }
  | { type: 'leave_requested' }
  | { type: 'ping_sent' }
  /** Нас вытеснила другая вкладка с тем же токеном. */
  | { type: 'displaced' }
  /** Человек нажал «продолжить здесь» на вытесненной вкладке. */
  | { type: 'resumed' }
  | { type: 'dismiss_toast' }
  | { type: 'watch_again' };

export interface ReduceContext {
  /** Локальное время, мс. */
  now: number;
  cooldownSec?: number;
}

let toastSeq = 0;

function toastFrom(msg: ErrorMsg, now: number): Toast | undefined {
  const p = describeErrorMessage(msg);
  if (p.severity === 'silent') return undefined;
  const message = msg.message ?? '';
  const useServerText = !p.preferOwnCopy && message && message !== p.title;
  return {
    id: ++toastSeq,
    code: msg.code,
    title: p.title,
    // Сервер прислал свой текст — обычно он конкретнее нашего общего.
    hint: useServerText ? message : p.hint,
    severity: p.severity,
    ttlMs: p.ttlMs,
    createdAt: now,
  };
}

export function reduce(
  state: ClientState,
  event: ClientEvent,
  ctx: ReduceContext
): ClientState {
  const { now } = ctx;
  const cooldownSec = ctx.cooldownSec ?? CONFIG_DEFAULTS.cooldown_sec;

  switch (event.type) {
    case 'socket_open':
      return { ...state, online: true, everConnected: true };

    case 'socket_closed':
      // Ход НЕ теряем: контракт даёт RECONNECT_GRACE = 10 с на возврат
      // с тем же токеном. Фазу оставляем, сервер поправит нас после `hello`.
      return { ...state, online: false };

    case 'displaced':
      // Ход за токеном сохраняется — просто рулит теперь другая вкладка.
      return { ...state, phase: 'displaced', online: false, toast: null };

    case 'resumed':
      return state.phase === 'displaced'
        ? { ...state, phase: 'connecting' }
        : state;

    case 'enqueue_requested':
      return { ...state, pendingEnqueue: true, toast: null };

    case 'release_requested':
      // Ждём `revoked` от сервера, но кнопку гасим сразу.
      return { ...state, pendingEnqueue: false };

    case 'leave_requested':
      // Ждём `queue` с position -1; кнопку гасим сразу.
      return { ...state, pendingEnqueue: false };

    case 'ping_sent':
      return { ...state, lastPingAt: now };

    case 'dismiss_toast':
      // Подсветка органа — часть того же сообщения: гаснет вместе с ним,
      // иначе слайдер остаётся жёлтым до конца хода без всякой причины.
      return { ...state, toast: null, highlight: null };

    case 'watch_again':
      // С экрана «время вышло» — обратно к наблюдению.
      return state.phase === 'expired'
        ? { ...state, phase: 'observer', lastRevoke: null }
        : state;

    case 'server':
      return reduceServer(state, event.msg, now, cooldownSec);
  }
}

function reduceServer(
  state: ClientState,
  msg: ServerMessage,
  now: number,
  cooldownSec: number
): ClientState {
  switch (msg.t) {
    case 'state': {
      const estop = msg.robot === 'estop';
      return settle({
        ...state,
        robot: msg.robot,
        joints: msg.joints ?? {},
        ee: msg.ee ?? null,
        lastStateAt: now,
        // Плашку аварийной остановки снимаем сами, как только робот ожил.
        estop,
      });
    }

    case 'queue': {
      const queue: QueueInfo = {
        position: msg.position,
        ahead: msg.ahead,
        etaSec: msg.eta_sec,
        queueLength: msg.queue_length,
      };

      if (msg.you_control) {
        return { ...state, queue, phase: 'controlling', pendingEnqueue: false };
      }
      if (msg.position >= 1) {
        return { ...state, queue, phase: 'queued', pendingEnqueue: false };
      }
      // position === -1 (наблюдатель вне очереди) либо 0 без you_control.
      // Экран «время вышло» не сбиваем — человек его ещё читает.
      const phase: Phase =
        state.phase === 'expired' || state.phase === 'displaced'
          ? state.phase
          : 'observer';
      return settle({ ...state, queue, phase, pendingEnqueue: false });
    }

    case 'granted':
      return {
        ...state,
        phase: 'controlling',
        control: { durationSec: msg.duration_sec, expiresAt: msg.expires_at },
        lastRevoke: null,
        pendingEnqueue: false,
        highlight: null,
        toast: null,
      };

    case 'revoked': {
      const copy = describeRevoke(msg.reason);
      return {
        ...state,
        phase: 'expired',
        control: null,
        lastRevoke: msg.reason,
        pendingEnqueue: false,
        highlight: null,
        estop: msg.reason === 'estop' ? true : state.estop,
        cooldownUntilMs: copy.cooldown ? now + cooldownSec * 1000 : state.cooldownUntilMs,
      };
    }

    case 'pong':
      return state;

    case 'error':
      return reduceError(state, msg, now, cooldownSec);
  }
}

function reduceError(
  state: ClientState,
  msg: ErrorMsg,
  now: number,
  cooldownSec: number
): ClientState {
  const code = msg.code;
  const effect = describeError(code).effect;
  const toast = toastFrom(msg, now);
  const next: ClientState = { ...state };

  switch (effect) {
    case 'demote_to_observer':
      // Мы считали себя оператором, а сервер — нет. Верим серверу.
      next.control = null;
      next.phase =
        state.queue && state.queue.position >= 1 ? 'queued' : 'observer';
      break;

    case 'offer_enqueue':
      next.phase = state.phase === 'expired' ? 'expired' : 'observer';
      next.pendingEnqueue = false;
      next.control = null;
      break;

    case 'start_cooldown':
      // `retry_after_sec` из тела ошибки; текст разбираем только как страховку.
      next.cooldownUntilMs = now + parseCooldownSeconds(msg, cooldownSec) * 1000;
      next.pendingEnqueue = false;
      break;

    case 'highlight_joint':
      // Сервер сам называет орган: `joint` для сустава, `axis` для оси джоггинга.
      next.highlight = msg.joint
        ? { kind: 'joint', name: msg.joint }
        : msg.axis
          ? { kind: 'axis', name: msg.axis }
          : null;
      break;

    case 'raise_estop':
      next.estop = true;
      next.pendingEnqueue = false;
      break;

    case 'backoff':
    case 'reconnect':
      // Побочный эффект исполняет транспорт; состояние не трогаем.
      break;

    case 'none':
      if (code === 'queue_full') next.pendingEnqueue = false;
      break;
  }

  if (toast) next.toast = toast;
  return next;
}

/** Если мы всё ещё «подключаемся», а сервер уже что-то сказал — скелетон снимаем. */
function settle(state: ClientState): ClientState {
  return state.phase === 'connecting' ? { ...state, phase: 'observer' } : state;
}

/* ------------------------------------------------------------------ селекторы */

export function cooldownRemainingSec(state: ClientState, now: number): number {
  if (state.cooldownUntilMs === null) return 0;
  return Math.max(0, Math.ceil((state.cooldownUntilMs - now) / 1000));
}

/** Секунды до конца хода. `expires_at` в контракте — UNIX-секунды. */
/**
 * Сколько секунд хода осталось.
 *
 * `null` означает «без ограничения» и это НЕ то же самое, что 0: ноль —
 * время вышло, null — его никто не считает. Возврат нуля в обоих случаях
 * зажигал бы «Время заканчивается» человеку, которого никто не торопит.
 */
export function controlRemainingSec(state: ClientState, nowMs: number): number | null {
  if (!state.control) return 0;
  if (state.control.expiresAt === null) return null;
  return Math.max(0, Math.ceil(state.control.expiresAt - nowMs / 1000));
}

export function canEnqueue(state: ClientState, now: number): boolean {
  if (!state.online) return false;
  if (state.estop) return false;
  if (state.pendingEnqueue) return false;
  if (state.phase !== 'observer' && state.phase !== 'expired') return false;
  return cooldownRemainingSec(state, now) === 0;
}

/**
 * Сервер замораживает движение, если не слышал нас дольше watchdog-таймаута.
 * Чаще всего это свёрнутая вкладка: браузер душит таймеры, `ping` перестаёт
 * уходить, рука встаёт. Экран обязан это признать, а не делать вид, что всё идёт.
 */
export function isMotionStalled(
  state: ClientState,
  now: number,
  watchdogSec: number
): boolean {
  if (state.phase !== 'controlling' || !state.online) return false;
  if (state.lastPingAt === null) return false;
  return now - state.lastPingAt > watchdogSec * 1000;
}

export function isController(state: ClientState): boolean {
  return state.phase === 'controlling';
}

/** Команды принимаются только своим ходом, при живом сокете и без estop. */
export function canCommand(state: ClientState): boolean {
  return state.phase === 'controlling' && state.online && !state.estop;
}
