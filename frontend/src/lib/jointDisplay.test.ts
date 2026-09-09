import { describe, expect, it } from 'vitest';
import {
  DEFAULT_SETTLE_MS,
  normalize,
  resolveJointDisplay,
  toDegrees,
} from './jointDisplay';

const NOW = 10_000;

describe('оптимистичный слайдер', () => {
  it('пока палец на слайдере, входящий state его НЕ дёргает', () => {
    const r = resolveJointDisplay({
      actual: 0.0, // робот ещё далеко
      target: { value: 1.2, setAt: NOW },
      dragging: true,
      now: NOW + 50,
    });
    expect(r.value).toBe(1.2);
    expect(r.keepTarget).toBe(true);
  });

  it('без своей цели слайдер просто показывает робота', () => {
    const r = resolveJointDisplay({
      actual: 0.33,
      target: null,
      dragging: false,
      now: NOW,
    });
    expect(r.value).toBe(0.33);
    expect(r.keepTarget).toBe(false);
  });

  it('после отпускания держит цель, пока робот доезжает', () => {
    const r = resolveJointDisplay({
      actual: 0.4,
      target: { value: 1.0, setAt: NOW },
      dragging: false,
      now: NOW + 200,
    });
    expect(r.value).toBe(1.0);
    expect(r.chasing).toBe(true);
    expect(r.keepTarget).toBe(true);
  });

  it('как только робот доехал — отдаёт управление данным робота', () => {
    const r = resolveJointDisplay({
      actual: 0.995,
      target: { value: 1.0, setAt: NOW },
      dragging: false,
      now: NOW + 500,
    });
    expect(r.value).toBe(0.995);
    expect(r.keepTarget).toBe(false);
    expect(r.chasing).toBe(false);
  });

  it('если робот так и не доехал, через settle-окно верим роботу', () => {
    // Иначе слайдер соврёт навсегда: например, цель зажали в предел.
    const r = resolveJointDisplay({
      actual: 0.2,
      target: { value: 1.5, setAt: NOW },
      dragging: false,
      now: NOW + DEFAULT_SETTLE_MS + 1,
    });
    expect(r.value).toBe(0.2);
    expect(r.keepTarget).toBe(false);
  });

  it('до прихода первого state показывает свою цель, а не ноль', () => {
    const r = resolveJointDisplay({
      actual: undefined,
      target: { value: 0.7, setAt: NOW },
      dragging: false,
      now: NOW + 10,
    });
    expect(r.value).toBe(0.7);
  });

  it('без данных вообще не падает', () => {
    const r = resolveJointDisplay({
      actual: undefined,
      target: null,
      dragging: false,
      now: NOW,
    });
    expect(r.value).toBe(0);
  });

  it('поток state на 10 Гц не вызывает дребезга около цели', () => {
    const target = { value: 0.5, setAt: NOW };
    let keep = true;
    // Робот подъезжает к цели, замедляясь: ограничитель скорости именно так и ведёт.
    for (let i = 0; i < 20 && keep; i++) {
      const actual = 0.5 - 0.5 * Math.pow(0.7, i);
      const r = resolveJointDisplay({
        actual,
        target,
        dragging: false,
        now: NOW + i * 100,
      });
      keep = r.keepTarget;
      // Пока держим цель, показываем ровно её — без скачков.
      if (keep) expect(r.value).toBe(0.5);
    }
    expect(keep).toBe(false); // в итоге переключились на робота
  });
});

describe('вспомогательные преобразования', () => {
  it('normalize укладывает значение в 0..1', () => {
    expect(normalize(0, -1, 1)).toBeCloseTo(0.5);
    expect(normalize(-5, -1, 1)).toBe(0);
    expect(normalize(5, -1, 1)).toBe(1);
  });

  it('normalize не делит на ноль при вырожденных пределах', () => {
    expect(normalize(1, 2, 2)).toBe(0);
  });

  it('toDegrees переводит радианы в градусы', () => {
    expect(toDegrees(Math.PI)).toBeCloseTo(180);
    expect(toDegrees(0)).toBe(0);
  });
});
