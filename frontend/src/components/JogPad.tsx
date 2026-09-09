import { useEffect, useRef, useState } from 'react';
import type { Axis } from '../lib/workspace';

/**
 * Джойстик для декартова джоггинга.
 *
 * `jog_ee` в контракте — ПРИРАЩЕНИЕ, поэтому пад работает как ручка газа:
 * пока палец отклонён, каждые 80 мс уходит маленький шаг в сторону отклонения.
 * Отпустили — движение прекращается само.
 */

const EMIT_INTERVAL_MS = 80;
const DEADZONE = 0.14;

interface Props {
  mode: 'xy' | 'z';
  disabled: boolean;
  /** Шаг из `config.ee_jog_step_m`. */
  step: number;
  onJog: (axis: 'x' | 'y' | 'z', delta: number) => void;
  /**
   * Ось, которую сервер назвал в `out_of_range.axis`. Именно её и подсвечиваем:
   * упереться можно в другую ось, а не в ту, по которой джогали.
   */
  highlightAxis?: Axis | null;
  /**
   * Направления у границы зоны — ключи `x+`, `y-`, `z+`… Рисуем полоску с той
   * стороны пада, куда рука уже не поедет: это видно раньше, чем нажмёшь.
   */
  blocked?: ReadonlySet<string>;
  labels: { up: string; down: string; left?: string; right?: string };
}

const NO_BLOCKS: ReadonlySet<string> = new Set<string>();

export function JogPad({
  mode,
  disabled,
  step,
  onJog,
  highlightAxis = null,
  blocked = NO_BLOCKS,
  labels,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const vector = useRef({ x: 0, y: 0 });
  const [knob, setKnob] = useState({ x: 0, y: 0 });
  const [active, setActive] = useState(false);

  useEffect(() => {
    if (!active || disabled) return;
    const id = setInterval(() => {
      const { x, y } = vector.current;
      if (mode === 'xy') {
        // Вверх по экрану = вперёд от оператора = +x. Вправо = +y.
        if (Math.abs(y) > DEADZONE) onJog('x', -y * step);
        if (Math.abs(x) > DEADZONE) onJog('y', x * step);
      } else {
        if (Math.abs(y) > DEADZONE) onJog('z', -y * step);
      }
    }, EMIT_INTERVAL_MS);
    return () => clearInterval(id);
  }, [active, disabled, mode, onJog, step]);

  const update = (e: React.PointerEvent) => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const nx = mode === 'xy' ? ((e.clientX - r.left) / r.width) * 2 - 1 : 0;
    const ny = ((e.clientY - r.top) / r.height) * 2 - 1;
    const clampUnit = (v: number) => Math.max(-1, Math.min(1, v));
    vector.current = { x: clampUnit(nx), y: clampUnit(ny) };
    setKnob(vector.current);
  };

  const start = (e: React.PointerEvent) => {
    if (disabled) return;
    // Захват пальца — удобство, а не необходимость (жест и так держит
    // touch-action: none). Если браузер откажет, пад обязан работать дальше.
    try {
      (e.target as Element).setPointerCapture?.(e.pointerId);
    } catch {
      /* нечего захватывать — не беда */
    }
    setActive(true);
    update(e);
  };

  const end = () => {
    setActive(false);
    vector.current = { x: 0, y: 0 };
    setKnob({ x: 0, y: 0 });
  };

  const isZ = mode === 'z';
  // Пад отвечает за свои оси: xy — за x и y, z — только за z.
  const owns = isZ ? highlightAxis === 'z' : highlightAxis === 'x' || highlightAxis === 'y';
  // Верх пада — это +x (вперёд) или +z (выше), низ — минус; вправо — +y.
  const edge = {
    up: blocked.has(isZ ? 'z+' : 'x+'),
    down: blocked.has(isZ ? 'z-' : 'x-'),
    left: !isZ && blocked.has('y-'),
    right: !isZ && blocked.has('y+'),
  };

  return (
    <div
      ref={ref}
      // touch-action: none — иначе жест уедет в скролл страницы вместо джоггинга.
      className={`grab relative select-none overflow-hidden rounded-2xl border-2 transition-colors ${
        isZ ? 'w-24' : 'flex-1 aspect-square'
      } ${
        disabled
          ? 'border-ink-200 bg-ink-100 opacity-50 dark:border-ink-800 dark:bg-ink-900'
          : owns
            ? 'border-amber-400 bg-amber-50 dark:border-amber-500/60 dark:bg-amber-500/10'
            : active
              ? 'border-accent-500 bg-accent-500/10'
              : 'border-ink-200 bg-white dark:border-ink-800 dark:bg-ink-900'
      }`}
      onPointerDown={start}
      onPointerMove={(e) => active && update(e)}
      onPointerUp={end}
      onPointerCancel={end}
      onLostPointerCapture={end}
    >
      {/* Перекрестье */}
      <div className="pointer-events-none absolute inset-0">
        {!isZ && (
          <div className="absolute left-1/2 top-0 h-full w-px -translate-x-1/2 bg-ink-200 dark:bg-ink-800" />
        )}
        <div className="absolute left-0 top-1/2 h-px w-full -translate-y-1/2 bg-ink-200 dark:bg-ink-800" />
      </div>

      {/* Стенка зоны: с этой стороны рука уже на границе и дальше не поедет. */}
      <div className="pointer-events-none absolute inset-0">
        {edge.up && <span className="absolute inset-x-0 top-0 h-1.5 bg-amber-400" />}
        {edge.down && (
          <span className="absolute inset-x-0 bottom-0 h-1.5 bg-amber-400" />
        )}
        {edge.left && <span className="absolute inset-y-0 left-0 w-1.5 bg-amber-400" />}
        {edge.right && (
          <span className="absolute inset-y-0 right-0 w-1.5 bg-amber-400" />
        )}
      </div>

      {/* Подписи направлений — единственный текст, который тут нужен. */}
      <span className="pointer-events-none absolute left-1/2 top-2 -translate-x-1/2 text-xs font-medium text-ink-400">
        {labels.up}
      </span>
      <span className="pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 text-xs font-medium text-ink-400">
        {labels.down}
      </span>
      {!isZ && (
        <>
          <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-xs font-medium text-ink-400">
            {labels.left}
          </span>
          <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-xs font-medium text-ink-400">
            {labels.right}
          </span>
        </>
      )}

      {/* Сам «шарик» */}
      <div
        className={`pointer-events-none absolute h-14 w-14 rounded-full border-4 border-white shadow-lg transition-colors ${
          active ? 'bg-accent-500' : 'bg-ink-300 dark:bg-ink-700'
        }`}
        style={{
          left: `calc(50% + ${(isZ ? 0 : knob.x) * 34}% - 1.75rem)`,
          top: `calc(50% + ${knob.y * 34}% - 1.75rem)`,
          transition: active ? 'none' : 'left 160ms ease-out, top 160ms ease-out',
        }}
      />
    </div>
  );
}
