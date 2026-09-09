import { useEffect, useState } from 'react';
import { useNow } from '../hooks/useNow';
import {
  normalize,
  resolveJointDisplay,
  toDegrees,
  type JointTarget,
} from '../lib/jointDisplay';
import type { JointConfig } from '../lib/protocol';

interface Props {
  joint: JointConfig;
  /** Реальное положение из потока `state`. */
  actual: number | undefined;
  disabled: boolean;
  /** Сервер ответил `out_of_range` именно по этому суставу. */
  highlighted: boolean;
  onChange: (value: number) => void;
}

export function JointSlider({ joint, actual, disabled, highlighted, onChange }: Props) {
  const [target, setTarget] = useState<JointTarget | null>(null);
  const [dragging, setDragging] = useState(false);

  // Часы нужны только пока мы ждём, что робот доедет до цели.
  const now = useNow(100, target !== null);

  const { value, keepTarget, chasing } = resolveJointDisplay({
    actual,
    target,
    dragging,
    now,
  });

  useEffect(() => {
    if (!keepTarget && target !== null) setTarget(null);
  }, [keepTarget, target]);

  // Палец мог уйти за пределы слайдера — ловим отпускание на всём окне,
  // иначе слайдер навсегда останется «зажатым».
  useEffect(() => {
    if (!dragging) return;
    const end = () => setDragging(false);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
    return () => {
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
    };
  }, [dragging]);

  const commit = (next: number) => {
    setTarget({ value: next, setAt: Date.now() });
    onChange(next);
  };

  const fill = normalize(value, joint.min, joint.max) * 100;
  const ghost = actual === undefined ? null : normalize(actual, joint.min, joint.max) * 100;
  const accent = highlighted ? '#f59e0b' : disabled ? '#8592aa' : '#16c47f';

  return (
    <div
      className={`rounded-2xl border px-4 py-2 transition-colors ${
        highlighted
          ? 'border-amber-400 bg-amber-50 dark:border-amber-500/60 dark:bg-amber-500/10'
          : 'border-ink-200 bg-white dark:border-ink-800 dark:bg-ink-900'
      }`}
    >
      <div className="flex items-baseline justify-between">
        <span className="text-[15px] font-semibold">{joint.label}</span>
        <span className="font-mono text-xs tabular-nums text-ink-500 dark:text-ink-400">
          {toDegrees(value).toFixed(0)}°
          {chasing && <span className="ml-1 opacity-60">→</span>}
        </span>
      </div>

      <div className="relative">
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
          step={(joint.max - joint.min) / 400}
          value={value}
          disabled={disabled}
          aria-label={joint.label}
          onPointerDown={() => setDragging(true)}
          onPointerUp={() => setDragging(false)}
          onPointerCancel={() => setDragging(false)}
          onChange={(e) => commit(Number(e.target.value))}
        />

        {/* Призрак: где рука находится на самом деле, пока догоняет цель. */}
        {ghost !== null && chasing && (
          <span
            aria-hidden
            className="pointer-events-none absolute top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-full bg-ink-400/70"
            style={{ left: `calc(${ghost}% - 1.5px)` }}
          />
        )}
      </div>
    </div>
  );
}
