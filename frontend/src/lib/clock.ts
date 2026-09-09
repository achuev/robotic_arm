/**
 * Тонкая обёртка над временем и таймерами.
 * Внедряется во все модули с таймингом, чтобы тесты шли без фейковых глобалов
 * и без реального ожидания.
 */
export interface Clock {
  now(): number;
  setTimeout(fn: () => void, ms: number): TimerHandle;
  clearTimeout(handle: TimerHandle): void;
}

export type TimerHandle = number;

export const systemClock: Clock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms) as unknown as TimerHandle,
  clearTimeout: (h) => clearTimeout(h as unknown as ReturnType<typeof setTimeout>),
};

/** Ручные часы для тестов: время двигается только вызовом `advance`. */
export class ManualClock implements Clock {
  private t: number;
  private seq = 1;
  private timers = new Map<TimerHandle, { at: number; fn: () => void }>();

  constructor(start = 0) {
    this.t = start;
  }

  now(): number {
    return this.t;
  }

  setTimeout(fn: () => void, ms: number): TimerHandle {
    const handle = this.seq++;
    this.timers.set(handle, { at: this.t + Math.max(0, ms), fn });
    return handle;
  }

  clearTimeout(handle: TimerHandle): void {
    this.timers.delete(handle);
  }

  /** Двигает время вперёд, по пути выполняя все сработавшие таймеры по порядку. */
  advance(ms: number): void {
    const target = this.t + ms;
    for (;;) {
      let nextHandle: TimerHandle | null = null;
      let nextAt = Infinity;
      for (const [h, timer] of this.timers) {
        if (timer.at <= target && timer.at < nextAt) {
          nextAt = timer.at;
          nextHandle = h;
        }
      }
      if (nextHandle === null) break;
      const timer = this.timers.get(nextHandle)!;
      this.timers.delete(nextHandle);
      this.t = timer.at;
      timer.fn();
    }
    this.t = target;
  }

  get pendingTimers(): number {
    return this.timers.size;
  }
}
