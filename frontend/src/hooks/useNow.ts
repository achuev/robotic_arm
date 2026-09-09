import { useEffect, useState } from 'react';

/**
 * Тикающие часы для обратных отсчётов.
 * `active = false` останавливает таймер — незачем перерисовывать экран,
 * на котором ничего не отсчитывается.
 */
export function useNow(intervalMs = 250, active = true): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs, active]);

  return now;
}
