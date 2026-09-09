import { systemClock, type Clock, type TimerHandle } from './clock';
import type { ClientMessage } from './protocol';

/**
 * Исходящая очередь с троттлингом и склейкой по ключу.
 *
 * Зачем: сервер режет всё, что выше `max_msg_per_sec`, и считает при этом ВСЕ
 * входящие — включая `ping` и `hello`. Палец по слайдеру генерирует сотни
 * событий в секунду, поэтому мы держим долю от серверного лимита и по каждому
 * ключу («сустав shoulder_pan», «ось x») храним только САМОЕ СВЕЖЕЕ значение:
 * команды абсолютные, промежуточные никому не нужны.
 */

/**
 * Какую долю серверного лимита занимаем. Треть оставляем на `ping`, `hello`,
 * пресеты и на расхождение часов клиента с сервером — иначе на границе лимита
 * мы бы стабильно ловили `rate_limited`.
 */
export const RATE_BUDGET = 2 / 3;

/** Целевая частота из серверного лимита. */
export function targetHzFor(maxMsgPerSec: number): number {
  const limit = Number.isFinite(maxMsgPerSec) && maxMsgPerSec > 0 ? maxMsgPerSec : 30;
  return Math.max(2, Math.floor(limit * RATE_BUDGET));
}

/** Минимальный интервал между исходящими для заданного серверного лимита. */
export function intervalForLimit(maxMsgPerSec: number): number {
  return 1000 / targetHzFor(maxMsgPerSec);
}

const MAX_INTERVAL_MS = 250;
const BACKOFF_FACTOR = 1.5;
const RECOVERY_FACTOR = 1.2;
const RECOVERY_AFTER_MS = 3000;

export interface OutboundOptions {
  send: (msg: ClientMessage) => void;
  clock?: Clock;
  /** Серверный лимит из `config.max_msg_per_sec`. */
  maxMsgPerSec?: number;
  /** Прямое задание интервала — приоритетнее лимита; используется в тестах. */
  intervalMs?: number;
}

export class OutboundQueue {
  private readonly send: (msg: ClientMessage) => void;
  private readonly clock: Clock;
  private readonly baseInterval: number;

  private interval: number;
  private pending = new Map<string, ClientMessage>();
  private timer: TimerHandle | null = null;
  private lastSentAt = -Infinity;
  private lastRateLimitedAt = -Infinity;
  private paused = false;

  /** Счётчики для тестов и диагностики. */
  sentCount = 0;
  droppedCount = 0;

  constructor(opts: OutboundOptions) {
    this.send = opts.send;
    this.clock = opts.clock ?? systemClock;
    this.baseInterval =
      opts.intervalMs ?? intervalForLimit(opts.maxMsgPerSec ?? 30);
    this.interval = this.baseInterval;
  }

  get currentIntervalMs(): number {
    return this.interval;
  }

  get pendingCount(): number {
    return this.pending.size;
  }

  /**
   * Кладёт сообщение в очередь. Повторный push с тем же ключом ЗАМЕЩАЕТ
   * предыдущее — старая цель уже неактуальна.
   */
  push(key: string, msg: ClientMessage): void {
    if (this.paused) return;
    if (this.pending.has(key)) this.droppedCount++;
    this.pending.set(key, msg);
    this.schedule();
  }

  /**
   * Отправляет вне очереди: `hello`, `enqueue`, `release`, `preset`, `ping`.
   * Такие сообщения редки и не должны ждать хвост слайдеров.
   */
  sendImmediate(msg: ClientMessage): void {
    this.lastSentAt = this.clock.now();
    this.sentCount++;
    this.send(msg);
  }

  /** Сервер прислал `rate_limited` — контракт велит молча притормозить. */
  noteRateLimited(): void {
    this.lastRateLimitedAt = this.clock.now();
    this.interval = Math.min(MAX_INTERVAL_MS, this.interval * BACKOFF_FACTOR);
  }

  /** Выбрасывает всё несделанное: ход потерян, старые цели слать некуда. */
  clear(): void {
    this.pending.clear();
    if (this.timer !== null) {
      this.clock.clearTimeout(this.timer);
      this.timer = null;
    }
  }

  /** Сокет закрылся: копить бессмысленно, при возврате состояние пришлют заново. */
  pause(): void {
    this.paused = true;
    this.clear();
  }

  resume(): void {
    this.paused = false;
    // Даём отправить сразу после переподключения, не дожидаясь интервала.
    this.lastSentAt = -Infinity;
  }

  private schedule(): void {
    if (this.timer !== null || this.pending.size === 0) return;
    const wait = Math.max(0, this.lastSentAt + this.interval - this.clock.now());
    this.timer = this.clock.setTimeout(() => {
      this.timer = null;
      this.flush();
    }, wait);
  }

  private flush(): void {
    const now = this.clock.now();

    // Давно не ругались — потихоньку возвращаемся к целевым 20 Гц.
    if (
      this.interval > this.baseInterval &&
      now - this.lastRateLimitedAt > RECOVERY_AFTER_MS
    ) {
      this.interval = Math.max(this.baseInterval, this.interval / RECOVERY_FACTOR);
    }

    const first = this.pending.keys().next();
    if (first.done) return;
    const key = first.value;
    const msg = this.pending.get(key)!;
    this.pending.delete(key);

    this.lastSentAt = now;
    this.sentCount++;
    this.send(msg);

    this.schedule();
  }
}

/* ------------------------------------------------------------- ключи склейки */

export const outboundKey = {
  joint: (name: string) => `set_joint:${name}`,
  joints: () => 'set_joints',
  jog: (axis: 'x' | 'y' | 'z') => `jog_ee:${axis}`,
  gripper: () => 'gripper',
};
