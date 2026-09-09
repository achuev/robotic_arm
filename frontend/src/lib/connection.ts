import { backoffDelay, type BackoffOptions } from './backoff';
import { systemClock, type Clock, type TimerHandle } from './clock';
import { describeError } from './errors';
import {
  initialState,
  reduce,
  type ClientEvent,
  type ClientState,
} from './machine';
import { OutboundQueue, outboundKey } from './outbound';
import {
  CLOSE_DISPLACED,
  CONFIG_DEFAULTS,
  clamp,
  parseServerMessage,
  type ClientMessage,
  type JointConfig,
  type PresetName,
} from './protocol';

/* ------------------------------------------------------------ сокет-абстракция */

/** Минимум, который нам нужен от WebSocket. Браузерный WebSocket подходит как есть. */
export interface SocketLike {
  send(data: string): void;
  close(): void;
  onopen: ((ev?: unknown) => void) | null;
  onclose: ((ev?: { code?: number }) => void) | null;
  onerror: ((ev?: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
}

export type SocketFactory = (url: string) => SocketLike;

const browserSocketFactory: SocketFactory = (url) =>
  new WebSocket(url) as unknown as SocketLike;

/* ------------------------------------------------------------------ настройки */

export interface ConnectionOptions {
  url: string;
  /** Тот же токен после обрыва = ход сохраняется (RECONNECT_GRACE). */
  getToken: () => string;
  joints?: JointConfig[];
  cooldownSec?: number;
  /** `config.watchdog_timeout_sec`: от него считается период `ping`. */
  watchdogSec?: number;
  /** `config.max_msg_per_sec`: от него считается троттлинг исходящих. */
  maxMsgPerSec?: number;
  clock?: Clock;
  socketFactory?: SocketFactory;
  backoff?: BackoffOptions;
  /** Нет ни одного `state` дольше этого — считаем сокет мёртвым и передёргиваем. */
  staleStateMs?: number;
  /** Сокет не открылся за это время — бросаем попытку и начинаем новую. */
  connectTimeoutMs?: number;
  /** Сколько соединение должно прожить, чтобы считаться удачным. */
  stableAfterMs?: number;
}

/**
 * Сколько `ping` укладывается в watchdog-окно. Три — чтобы пара пропущенных
 * тиков таймера не роняла движение.
 */
const PINGS_PER_WATCHDOG = 3;
const MIN_PING_INTERVAL_MS = 500;

const DEFAULT_STALE_STATE_MS = 5000;
const DEFAULT_CONNECT_TIMEOUT_MS = 4000;
export const DEFAULT_STABLE_AFTER_MS = 3000;

/* ------------------------------------------------------------------ менеджер */

export class ConnectionManager {
  private readonly opts: ConnectionOptions;
  private readonly clock: Clock;
  private readonly makeSocket: SocketFactory;
  private readonly staleStateMs: number;
  private readonly connectTimeoutMs: number;
  private readonly stableAfterMs: number;
  private readonly pingIntervalMs: number;

  private socket: SocketLike | null = null;
  private reconnectTimer: TimerHandle | null = null;
  private staleTimer: TimerHandle | null = null;
  private pingTimer: TimerHandle | null = null;
  private connectTimer: TimerHandle | null = null;
  private stableTimer: TimerHandle | null = null;
  private attempt = 0;
  private stopped = true;
  private onVisibility: (() => void) | null = null;

  private state: ClientState = initialState;
  private listeners = new Set<(s: ClientState) => void>();

  readonly outbound: OutboundQueue;

  /** Диагностика для тестов. */
  connectCount = 0;
  lastDelayMs = 0;

  constructor(options: ConnectionOptions) {
    this.opts = options;
    this.clock = options.clock ?? systemClock;
    this.makeSocket = options.socketFactory ?? browserSocketFactory;
    this.staleStateMs = options.staleStateMs ?? DEFAULT_STALE_STATE_MS;
    this.connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this.stableAfterMs = options.stableAfterMs ?? DEFAULT_STABLE_AFTER_MS;

    const watchdogSec = options.watchdogSec ?? CONFIG_DEFAULTS.watchdog_timeout_sec;
    this.pingIntervalMs = Math.max(
      MIN_PING_INTERVAL_MS,
      Math.floor((watchdogSec * 1000) / PINGS_PER_WATCHDOG)
    );

    this.outbound = new OutboundQueue({
      clock: this.clock,
      maxMsgPerSec: options.maxMsgPerSec ?? CONFIG_DEFAULTS.max_msg_per_sec,
      send: (msg) => this.rawSend(msg),
    });
  }

  /* ----------------------------------------------------------- подписка */

  getState(): ClientState {
    return this.state;
  }

  subscribe(fn: (s: ClientState) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private dispatch(event: ClientEvent): void {
    const next = reduce(this.state, event, {
      now: this.clock.now(),
      cooldownSec: this.opts.cooldownSec,
    });
    if (next === this.state) return;
    this.state = next;
    for (const fn of this.listeners) fn(next);
  }

  /* ----------------------------------------------------------- жизненный цикл */

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.watchVisibility();
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.clearAllTimers();
    this.unwatchVisibility();
    this.outbound.pause();
    this.detachAndClose();
  }

  /** Возврат с экрана «управление продолжено в другой вкладке». */
  resume(): void {
    if (!this.stopped) return;
    this.dispatch({ type: 'resumed' });
    this.stopped = false;
    this.attempt = 0;
    this.watchVisibility();
    this.open();
  }

  /** Принудительно передёрнуть соединение (например, после `bad_message`). */
  reconnectNow(): void {
    this.detachAndClose();
    this.handleClose();
  }

  private open(): void {
    if (this.stopped) return;
    this.connectCount++;
    const socket = this.makeSocket(this.opts.url);
    this.socket = socket;

    // Попытка не должна висеть вечно: если сокет не открылся, начинаем заново.
    this.clearTimer('connectTimer');
    this.connectTimer = this.clock.setTimeout(() => {
      this.connectTimer = null;
      if (this.socket === socket) this.reconnectNow();
    }, this.connectTimeoutMs);

    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.clearTimer('connectTimer');
      this.outbound.resume();

      // Первое сообщение всегда `hello` с тем же токеном — этим и держится ход.
      this.rawSend({ t: 'hello', token: this.opts.getToken() });
      this.dispatch({ type: 'socket_open' });

      // Сразу кормим watchdog, не дожидаясь первого тика heartbeat.
      this.sendPing();
      this.armStale();
      this.armPing();

      // Счётчик попыток обнуляем не сразу: соединение, которое живёт секунду
      // и обрывается, — это не успех. Иначе две вкладки, вытесняющие друг
      // друга, крутили бы реконнект на полной скорости бесконечно.
      this.clearTimer('stableTimer');
      this.stableTimer = this.clock.setTimeout(() => {
        this.stableTimer = null;
        this.attempt = 0;
      }, this.stableAfterMs);
    };

    socket.onmessage = (ev) => {
      if (this.socket !== socket) return;
      if (typeof ev.data !== 'string') return;
      const msg = parseServerMessage(ev.data);
      // Неизвестный `t` игнорируется и соединение не рвётся — контракт §3.
      if (!msg) return;

      if (msg.t === 'state') this.armStale();

      if (msg.t === 'error') {
        const effect = describeError(msg.code).effect;
        if (effect === 'backoff') this.outbound.noteRateLimited();
        if (effect === 'reconnect') {
          this.dispatch({ type: 'server', msg });
          this.reconnectNow();
          return;
        }
        if (effect === 'demote_to_observer' || effect === 'raise_estop') {
          // Команды в трубе уже неактуальны.
          this.outbound.clear();
        }
      }

      if (msg.t === 'revoked') this.outbound.clear();

      this.dispatch({ type: 'server', msg });
    };

    socket.onerror = () => {
      /* onclose придёт следом, там и обработаем */
    };

    socket.onclose = (ev) => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (ev?.code === CLOSE_DISPLACED) {
        this.handleDisplaced();
        return;
      }
      this.handleClose();
    };
  }

  /**
   * Нас вытеснила вторая вкладка с тем же токеном. Переподключаться НЕЛЬЗЯ:
   * мы вытесним её в ответ, она — нас, и так до бесконечности. Ход при этом
   * не потерян, он просто «переехал», поэтому и паниковать не о чем.
   */
  private handleDisplaced(): void {
    this.stopped = true;
    this.clearAllTimers();
    this.unwatchVisibility();
    this.outbound.pause();
    this.dispatch({ type: 'displaced' });
  }

  private handleClose(): void {
    this.clearTimer('staleTimer');
    this.clearTimer('pingTimer');
    this.clearTimer('connectTimer');
    this.clearTimer('stableTimer');
    this.outbound.pause();
    this.dispatch({ type: 'socket_closed' });
    if (this.stopped) return;

    this.attempt++;
    const delay = backoffDelay(this.attempt, this.opts.backoff);
    this.lastDelayMs = delay;
    this.clearTimer('reconnectTimer');
    this.reconnectTimer = this.clock.setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delay);
  }

  private detachAndClose(): void {
    const socket = this.socket;
    this.socket = null;
    if (!socket) return;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    try {
      socket.close();
    } catch {
      /* уже мёртв */
    }
  }

  /* ----------------------------------------------------------- таймеры */

  private armStale(): void {
    this.clearTimer('staleTimer');
    this.staleTimer = this.clock.setTimeout(() => {
      this.staleTimer = null;
      // Поток `state` идёт на 10 Гц. Тишина = сокет висит, но не закрылся.
      this.reconnectNow();
    }, this.staleStateMs);
  }

  /**
   * `ping` не реже 1 Гц — иначе сервер заморозит движение руки.
   * Идёт всегда, даже когда человек ничего не трогает: watchdog следит за
   * живостью соединения, а не за потоком команд.
   */
  private armPing(): void {
    this.clearTimer('pingTimer');
    this.pingTimer = this.clock.setTimeout(() => {
      this.pingTimer = null;
      if (!this.socket) return;
      this.sendPing();
      this.armPing();
    }, this.pingIntervalMs);
  }

  private sendPing(): void {
    if (!this.socket) return;
    this.outbound.sendImmediate({ t: 'ping' });
    this.dispatch({ type: 'ping_sent' });
  }

  /**
   * Вкладка вернулась из фона — таймеры там душатся, и сервер уже мог
   * заморозить руку. Пингуем немедленно, чтобы движение продолжилось.
   */
  private watchVisibility(): void {
    if (typeof document === 'undefined' || this.onVisibility) return;
    this.onVisibility = () => {
      if (document.visibilityState !== 'visible' || !this.socket) return;
      this.sendPing();
      this.armPing();
    };
    document.addEventListener('visibilitychange', this.onVisibility);
  }

  private unwatchVisibility(): void {
    if (typeof document === 'undefined' || !this.onVisibility) return;
    document.removeEventListener('visibilitychange', this.onVisibility);
    this.onVisibility = null;
  }

  private clearAllTimers(): void {
    this.clearTimer('reconnectTimer');
    this.clearTimer('staleTimer');
    this.clearTimer('pingTimer');
    this.clearTimer('connectTimer');
    this.clearTimer('stableTimer');
  }

  private clearTimer(
    field:
      | 'reconnectTimer'
      | 'staleTimer'
      | 'pingTimer'
      | 'connectTimer'
      | 'stableTimer'
  ): void {
    const h = this[field];
    if (h !== null) {
      this.clock.clearTimeout(h);
      this[field] = null;
    }
  }

  private rawSend(msg: ClientMessage): void {
    if (!this.socket) return;
    try {
      this.socket.send(JSON.stringify(msg));
    } catch {
      /* закрылся между проверкой и отправкой */
    }
  }

  /* ----------------------------------------------------------- команды UI */

  enqueue(): void {
    this.dispatch({ type: 'enqueue_requested' });
    this.outbound.sendImmediate({ t: 'enqueue' });
  }

  release(): void {
    this.dispatch({ type: 'release_requested' });
    this.outbound.clear();
    this.outbound.sendImmediate({ t: 'release' });
  }

  /** Выйти из очереди. Явное сообщение контракта; сервер ответит `queue` с -1. */
  leaveQueue(): void {
    this.dispatch({ type: 'leave_requested' });
    this.outbound.sendImmediate({ t: 'leave' });
  }

  /** Абсолютная цель сустава. Клэмпим локально, сервер клэмпит повторно. */
  setJoint(joint: string, position: number): void {
    const spec = this.opts.joints?.find((j) => j.name === joint);
    const value = spec ? clamp(position, spec.min, spec.max) : position;
    this.outbound.push(outboundKey.joint(joint), {
      t: 'set_joint',
      joint,
      position: value,
    });
  }

  setJoints(positions: Record<string, number>): void {
    this.outbound.push(outboundKey.joints(), { t: 'set_joints', positions });
  }

  /** Захват всюду в долях 0..1 — контракт §2, `"normalized": true`. */
  gripper(value: number): void {
    this.outbound.push(outboundKey.gripper(), {
      t: 'gripper',
      value: clamp(value, 0, 1),
    });
  }

  jogEe(axis: 'x' | 'y' | 'z', delta: number): void {
    this.outbound.push(outboundKey.jog(axis), { t: 'jog_ee', axis, delta });
  }

  preset(name: PresetName): void {
    this.outbound.clear();
    this.outbound.sendImmediate({ t: 'preset', name });
  }

  dismissToast(): void {
    this.dispatch({ type: 'dismiss_toast' });
  }

  watchAgain(): void {
    this.dispatch({ type: 'watch_again' });
  }
}
