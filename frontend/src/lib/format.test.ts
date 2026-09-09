import { describe, expect, it } from 'vitest';
import { formatWait, peopleAhead, plural } from './format';

describe('formatWait', () => {
  it('нулевое и отрицательное ожидание не пугает числами', () => {
    expect(formatWait(0)).toBe('вот-вот');
    expect(formatWait(-5)).toBe('вот-вот');
  });
  it('до минуты округляет до пятёрок', () => {
    expect(formatWait(12)).toBe('15 секунд');
    expect(formatWait(45)).toBe('45 секунд');
  });
  it('дальше переходит на минуты', () => {
    expect(formatWait(70)).toBe('около минуты');
    expect(formatWait(150)).toBe('около 3 минут');
    expect(formatWait(400)).toBe('больше 7 минут');
  });
});

describe('plural', () => {
  it('склоняет по русским правилам', () => {
    const p = (n: number) => plural(n, 'человек', 'человека', 'человек');
    expect(p(1)).toBe('человек');
    expect(p(2)).toBe('человека');
    expect(p(5)).toBe('человек');
    expect(p(11)).toBe('человек'); // не «11 человек» через «один»
    expect(p(21)).toBe('человек');
    expect(p(22)).toBe('человека');
    expect(p(14)).toBe('человек');
    expect(p(0)).toBe('человек');
  });

  it('peopleAhead собирает число со словом', () => {
    expect(peopleAhead(1)).toBe('1 человек');
    expect(peopleAhead(3)).toBe('3 человека');
    expect(peopleAhead(8)).toBe('8 человек');
  });
});
