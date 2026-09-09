interface Props {
  remainingSec: number;
  totalSec: number;
}

/** Порог, после которого таймер начинает кричать. */
export const URGENT_SEC = 15;

/**
 * Кольцо обратного отсчёта. У человека 90 секунд, и он должен видеть,
 * сколько осталось, не читая текста: кольцо тает, под конец краснеет и мигает.
 */
export function Countdown({ remainingSec, totalSec }: Props) {
  const total = totalSec > 0 ? totalSec : 1;
  const fraction = Math.max(0, Math.min(1, remainingSec / total));
  const urgent = remainingSec <= URGENT_SEC;

  const R = 22;
  const circumference = 2 * Math.PI * R;

  return (
    <div
      className={`relative h-[52px] w-[52px] shrink-0 ${urgent ? 'animate-pulse-urgent' : ''}`}
      role="timer"
      aria-label={`Осталось ${remainingSec} секунд`}
    >
      <svg viewBox="0 0 52 52" className="h-full w-full -rotate-90">
        <circle
          cx="26" cy="26" r={R}
          className="fill-none stroke-ink-200 dark:stroke-ink-800"
          strokeWidth="5"
        />
        <circle
          cx="26" cy="26" r={R}
          className={`fill-none ${urgent ? 'stroke-red-500' : 'stroke-accent-500'}`}
          strokeWidth="5"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - fraction)}
          style={{ transition: 'stroke-dashoffset 300ms linear' }}
        />
      </svg>
      <span
        className={`absolute inset-0 flex items-center justify-center text-lg font-bold tabular-nums ${
          urgent ? 'text-red-500' : ''
        }`}
      >
        {remainingSec}
      </span>
    </div>
  );
}
