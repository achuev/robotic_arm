/**
 * Нарастающая задержка переподключения.
 *
 * Подобрана под RECONNECT_GRACE = 10 с из docs/api.md §4: первые попытки должны
 * уложиться в это окно, иначе оператор потеряет ход на ровном месте.
 * 300 / 600 / 1200 / 2400 / 4800 мс — пять попыток за первые 9.3 с.
 */

export interface BackoffOptions {
  baseMs?: number;
  factor?: number;
  capMs?: number;
  /** Доля разброса, 0..1. Нужен, чтобы 20 телефонов не ломились в один момент. */
  jitter?: number;
  random?: () => number;
}

export const BACKOFF_DEFAULTS = {
  baseMs: 300,
  factor: 2,
  capMs: 10_000,
  jitter: 0.2,
};

/** `attempt` считается с 1. */
export function backoffDelay(attempt: number, opts: BackoffOptions = {}): number {
  const base = opts.baseMs ?? BACKOFF_DEFAULTS.baseMs;
  const factor = opts.factor ?? BACKOFF_DEFAULTS.factor;
  const cap = opts.capMs ?? BACKOFF_DEFAULTS.capMs;
  const jitter = opts.jitter ?? BACKOFF_DEFAULTS.jitter;
  const random = opts.random ?? Math.random;

  const n = Math.max(1, Math.floor(attempt));
  const raw = Math.min(cap, base * Math.pow(factor, n - 1));
  if (jitter <= 0) return Math.round(raw);

  const spread = raw * jitter;
  const value = raw - spread + random() * spread * 2;
  return Math.round(Math.min(cap, Math.max(0, value)));
}

/**
 * Сколько попыток успеет пройти за окно `windowMs`.
 * Используется в тестах как страховка: если кто-то поднимет baseMs,
 * тест на «обрыв на 10 секунд не теряет ход» упадёт.
 */
export function attemptsWithin(windowMs: number, opts: BackoffOptions = {}): number {
  let elapsed = 0;
  let attempts = 0;
  const noJitter = { ...opts, jitter: 0 };
  for (let n = 1; n < 100; n++) {
    elapsed += backoffDelay(n, noJitter);
    if (elapsed > windowMs) break;
    attempts++;
  }
  return attempts;
}
