import { describe, expect, it } from 'vitest';
import { attemptsWithin, backoffDelay, BACKOFF_DEFAULTS } from './backoff';

const RECONNECT_GRACE_MS = 10_000; // docs/api.md §4

describe('backoff переподключения', () => {
  it('растёт от попытки к попытке', () => {
    const delays = [1, 2, 3, 4, 5].map((n) => backoffDelay(n, { jitter: 0 }));
    for (let i = 1; i < delays.length; i++) {
      expect(delays[i]).toBeGreaterThan(delays[i - 1]);
    }
    expect(delays[0]).toBe(300);
    expect(delays[1]).toBe(600);
    expect(delays[2]).toBe(1200);
  });

  it('упирается в потолок и не растёт бесконечно', () => {
    expect(backoffDelay(50, { jitter: 0 })).toBe(BACKOFF_DEFAULTS.capMs);
    expect(backoffDelay(1000, { jitter: 0 })).toBe(BACKOFF_DEFAULTS.capMs);
  });

  it('успевает несколько попыток внутри RECONNECT_GRACE = 10 с', () => {
    // Иначе оператор теряет ход на десятисекундном обрыве метро.
    expect(attemptsWithin(RECONNECT_GRACE_MS)).toBeGreaterThanOrEqual(4);
  });

  it('первая попытка укладывается меньше чем в секунду', () => {
    expect(backoffDelay(1, { jitter: 0 })).toBeLessThan(1000);
  });

  it('джиттер разбрасывает задержку, но держит её около номинала', () => {
    const lo = backoffDelay(3, { jitter: 0.2, random: () => 0 });
    const hi = backoffDelay(3, { jitter: 0.2, random: () => 1 });
    const mid = backoffDelay(3, { jitter: 0 });
    expect(lo).toBeLessThan(mid);
    expect(hi).toBeGreaterThan(mid);
    expect(lo).toBeGreaterThanOrEqual(mid * 0.75);
    expect(hi).toBeLessThanOrEqual(mid * 1.25);
  });

  it('никогда не возвращает отрицательное значение', () => {
    for (let n = 1; n <= 20; n++) {
      expect(backoffDelay(n, { jitter: 1, random: () => 0 })).toBeGreaterThanOrEqual(0);
    }
  });
});
