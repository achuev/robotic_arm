/**
 * Оптимистичное отображение слайдера.
 *
 * Задача: слайдер обязан слушаться пальца мгновенно, но реальное положение
 * приходит в `state` 10 раз в секунду и ВСЕГДА отстаёт — ограничитель скорости
 * доводит сустав до цели плавно. Если тупо рисовать `state`, слайдер будет
 * выдирать из-под пальца и отпрыгивать назад после отпускания.
 *
 * Правило: пока палец на слайдере — рисуем цель и ничего не слушаем. После
 * отпускания продолжаем рисовать цель, пока робот до неё не доедет (или пока
 * не станет ясно, что не доедет — например, значение зажали в предел).
 */

export interface JointTarget {
  value: number;
  /** Локальное время в мс, когда цель поставили. */
  setAt: number;
}

export interface JointDisplayInput {
  /** Последнее известное положение из `state`. */
  actual: number | undefined;
  /** Куда мы попросили приехать, или null, если не просили. */
  target: JointTarget | null;
  /** Палец сейчас на элементе. */
  dragging: boolean;
  now: number;
  /** Насколько близко считается «доехал», рад. */
  toleranceRad?: number;
  /** Сколько ждём схождения, прежде чем сдаться и поверить роботу, мс. */
  settleMs?: number;
}

export interface JointDisplayResult {
  /** Что рисовать. */
  value: number;
  /** Нужно ли и дальше держать цель, или её можно забыть. */
  keepTarget: boolean;
  /** Робот ещё едет к цели — рисуем «призрак» реального положения. */
  chasing: boolean;
}

export const DEFAULT_TOLERANCE_RAD = 0.02;
export const DEFAULT_SETTLE_MS = 2500;

export function resolveJointDisplay(input: JointDisplayInput): JointDisplayResult {
  const tolerance = input.toleranceRad ?? DEFAULT_TOLERANCE_RAD;
  const settleMs = input.settleMs ?? DEFAULT_SETTLE_MS;
  const actual = input.actual;

  // Палец главнее всего: никакой входящий `state` не имеет права дёрнуть слайдер.
  if (input.dragging) {
    const value = input.target?.value ?? actual ?? 0;
    return { value, keepTarget: true, chasing: false };
  }

  if (!input.target) {
    return { value: actual ?? 0, keepTarget: false, chasing: false };
  }

  if (actual === undefined) {
    return { value: input.target.value, keepTarget: true, chasing: false };
  }

  const converged = Math.abs(actual - input.target.value) <= tolerance;
  if (converged) {
    // Доехали — дальше слайдер живёт по данным робота.
    return { value: actual, keepTarget: false, chasing: false };
  }

  if (input.now - input.target.setAt >= settleMs) {
    // Не доехал за отведённое время: цель зажали в предел или робот занят.
    // Верим роботу, иначе слайдер соврёт навсегда.
    return { value: actual, keepTarget: false, chasing: false };
  }

  return { value: input.target.value, keepTarget: true, chasing: true };
}

/** Позиция 0..1 внутри пределов сустава — для отрисовки трека. */
export function normalize(value: number, min: number, max: number): number {
  if (max <= min) return 0;
  const t = (value - min) / (max - min);
  return t < 0 ? 0 : t > 1 ? 1 : t;
}

/** Радианы в градусы, для подписи под слайдером. */
export function toDegrees(rad: number): number {
  return (rad * 180) / Math.PI;
}
