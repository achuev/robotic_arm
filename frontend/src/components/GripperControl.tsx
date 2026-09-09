import { useEffect, useState } from 'react';

import type { JointConfig } from '../lib/protocol';

interface Props {
  joint: JointConfig;
  /** Текущее раскрытие из `state`, в единицах сустава. */
  actual: number | undefined;
  disabled: boolean;
  onChange: (value: number) => void;
}

/**
 * Захват. Две крупные кнопки — это то, что прохожий понимает без
 * объяснений, но ими нельзя взять предмет: чтобы удержать, губки надо
 * свести ровно настолько, насколько нужно. Поэтому кнопки остались, а к
 * ним добавился ползунок.
 */
export function GripperControl({ joint, actual, disabled, onChange }: Props) {
  // Пока палец ведёт ползунок, показываем СВОЁ значение, а не пришедшее с
  // робота: иначе рука, догоняющая цель, тянет ручку назад под пальцем.
  const [pending, setPending] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);

  const measured = actual ?? joint.min;
  useEffect(() => {
    if (dragging || pending === null) return;
    // Отпустили — отдаём управление показаниям робота, когда он доехал.
    if (Math.abs(measured - pending) < (joint.max - joint.min) * 0.03) setPending(null);
  }, [measured, pending, dragging, joint.max, joint.min]);

  const raw = pending ?? measured;
  const span = joint.max - joint.min || 1;
  const openness = Math.min(1, Math.max(0, (raw - joint.min) / span));
  const isOpen = openness > 0.5;

  const commit = (next: number) => {
    setPending(next);
    onChange(next);
  };

  const accent = disabled ? '#8592aa' : '#f59e0b';
  const fill = openness * 100;
  const ghost = actual === undefined ? null
    : Math.min(1, Math.max(0, (actual - joint.min) / span)) * 100;
  const chasing = pending !== null && Math.abs(measured - pending) > span * 0.03;

  // Губки расходятся от 4 до 26 px.
  const gap = 4 + openness * 22;

  return (
    <div className="rounded-2xl border border-ink-200 bg-white p-4 dark:border-ink-800 dark:bg-ink-900">
      <div className="mb-3 flex items-center gap-3">
        <svg viewBox="0 0 80 56" className="h-12 w-16 shrink-0" aria-hidden>
          <rect x="36" y="38" width="8" height="16" rx="3" className="fill-ink-400" />
          <rect
            x={40 - gap - 7}
            y="6"
            width="7"
            height="34"
            rx="3"
            className={disabled ? 'fill-ink-400' : 'fill-amber-400'}
            style={{ transition: 'x 180ms ease-out' }}
          />
          <rect
            x={40 + gap}
            y="6"
            width="7"
            height="34"
            rx="3"
            className={disabled ? 'fill-ink-400' : 'fill-amber-400'}
            style={{ transition: 'x 180ms ease-out' }}
          />
        </svg>
        <div>
          <div className="text-[15px] font-semibold">{joint.label}</div>
          <div className="text-sm text-ink-500 dark:text-ink-400">
            раскрыт на {Math.round(openness * 100)}%
          </div>
        </div>
      </div>

      {/* Ползунок: промежуточное раскрытие, без него предмет не удержать. */}
      <div className="relative mb-3">
        <input
          type="range"
          className="joint-range grab [--empty:#d5dae3] dark:[--empty:#3a4051]"
          style={
            {
              '--track': `linear-gradient(to right, ${accent} 0%, ${accent} ${fill}%, var(--empty) ${fill}%, var(--empty) 100%)`,
              '--thumb': accent,
            } as React.CSSProperties
          }
          min={joint.min}
          max={joint.max}
          step={span / 200}
          value={raw}
          disabled={disabled}
          aria-label={`${joint.label}: раскрытие`}
          onPointerDown={() => setDragging(true)}
          onPointerUp={() => setDragging(false)}
          onPointerCancel={() => setDragging(false)}
          onChange={(e) => commit(Number(e.target.value))}
        />

        {/* Призрак: где губки на самом деле, пока догоняют цель. */}
        {ghost !== null && chasing && (
          <span
            aria-hidden
            className="pointer-events-none absolute top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-full bg-ink-400/70"
            style={{ left: `calc(${ghost}% - 1.5px)` }}
          />
        )}
      </div>

      <div className="grid grid-cols-2 gap-3">
        <button
          type="button"
          disabled={disabled}
          onClick={() => commit(joint.max)}
          className={`grab h-16 rounded-xl text-lg font-semibold transition active:scale-[0.97] disabled:opacity-40 ${
            isOpen
              ? 'bg-amber-400 text-ink-950'
              : 'border-2 border-ink-300 dark:border-ink-700'
          }`}
        >
          Открыть
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => commit(joint.min)}
          className={`grab h-16 rounded-xl text-lg font-semibold transition active:scale-[0.97] disabled:opacity-40 ${
            !isOpen
              ? 'bg-amber-400 text-ink-950'
              : 'border-2 border-ink-300 dark:border-ink-700'
          }`}
        >
          Сжать
        </button>
      </div>
    </div>
  );
}
