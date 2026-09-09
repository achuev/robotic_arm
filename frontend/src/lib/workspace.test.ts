import { describe, expect, it } from 'vitest';
import { describeErrorMessage } from './errors';
import { initialState, reduce, type ClientState } from './machine';
import type { Workspace } from './protocol';
import {
  axisEdge,
  blockedDirections,
  describeWorkspacePosition,
  isOutsideWorkspace,
  workspaceEdges,
} from './workspace';

/**
 * Зона живого стенда: по x граница 0.38, а нулевая поза даёт 0.391 —
 * рука включается СНАРУЖИ объявленной зоны. Ровно этот случай и разбираем.
 */
const WS: Workspace = { x: [0.05, 0.38], y: [-0.25, 0.25], z: [0.02, 0.35] };

const ZERO_POSE = { x: 0.391, y: 0, z: 0.12 };
const MIDDLE = { x: 0.2, y: 0, z: 0.15 };

describe('workspace: где рука относительно границ', () => {
  it('точка в середине зоны не даёт ни одной границы', () => {
    expect(workspaceEdges(MIDDLE, WS)).toEqual([]);
    expect(isOutsideWorkspace(MIDDLE, WS)).toBe(false);
    expect(describeWorkspacePosition(MIDDLE, WS)).toBeNull();
  });

  it('стартовая поза стенда распознаётся как выход за верхнюю границу x', () => {
    const edges = workspaceEdges(ZERO_POSE, WS);
    expect(edges).toHaveLength(1);
    expect(edges[0].axis).toBe('x');
    expect(edges[0].side).toBe(1);
    expect(edges[0].overshootM).toBeCloseTo(0.011, 6);
    expect(isOutsideWorkspace(ZERO_POSE, WS)).toBe(true);
  });

  it('точка ровно на границе — это «у края», но не «снаружи»', () => {
    const onEdge = { x: 0.38, y: 0, z: 0.15 };
    expect(workspaceEdges(onEdge, WS)).toHaveLength(1);
    expect(isOutsideWorkspace(onEdge, WS)).toBe(false);
  });

  it('несколько осей сразу перечисляются в порядке x, y, z', () => {
    const corner = { x: 0.39, y: -0.26, z: 0.36 };
    expect(workspaceEdges(corner, WS).map((e) => e.axis)).toEqual(['x', 'y', 'z']);
  });

  it('нет ee или нет workspace — молчим, а не гадаем', () => {
    expect(workspaceEdges(null, WS)).toEqual([]);
    expect(workspaceEdges(MIDDLE, null)).toEqual([]);
  });

  it('вырожденная зона (шлюз не отдал workspace) не даёт ложной тревоги', () => {
    const degenerate: Workspace = { x: [0, 0], y: [0, 0], z: [0, 0] };
    expect(workspaceEdges(MIDDLE, degenerate)).toEqual([]);
  });

  it('NaN в координате не превращается в границу', () => {
    expect(axisEdge('x', Number.NaN, [0.05, 0.38])).toBeNull();
  });

  it('заблокированные направления адресуются как ось+сторона', () => {
    const blocked = blockedDirections(workspaceEdges(ZERO_POSE, WS));
    expect(blocked.has('x+')).toBe(true);
    expect(blocked.has('x-')).toBe(false);
  });
});

describe('workspace: что читает человек', () => {
  it('стартовая поза объясняется как норма, а не как поломка', () => {
    const notice = describeWorkspacePosition(ZERO_POSE, WS);
    expect(notice?.kind).toBe('outside');
    // Ни «ошибки», ни «сбоя» — иначе первый же посетитель решит, что сломал.
    expect(`${notice?.title} ${notice?.hint}`).not.toMatch(/ошибк|сбой|недоступ/i);
    expect(notice?.hint).toContain('не поломка');
    // Подсказываем именно то направление, которое ВЕРНЁТ руку в зону.
    expect(notice?.hint).toContain('назад');
  });

  it('на границе — говорим, куда нельзя, и что остальное работает', () => {
    const notice = describeWorkspacePosition({ x: 0.379, y: 0, z: 0.15 }, WS);
    expect(notice?.kind).toBe('edge');
    expect(notice?.hint).toContain('вперёд');
    expect(notice?.hint).toContain('Остальные направления работают');
  });

  it('весь текст по-русски', () => {
    const notice = describeWorkspacePosition(ZERO_POSE, WS);
    expect(/[А-Яа-яЁё]/.test(notice?.title ?? '')).toBe(true);
    expect(/[А-Яа-яЁё]/.test(notice?.hint ?? '')).toBe(true);
  });
});

describe('out_of_range по оси подаётся спокойно', () => {
  const controlling: ClientState = {
    ...initialState,
    phase: 'controlling',
    online: true,
    everConnected: true,
  };

  function jogRefusal(axis: string, message = 'Точка вне рабочей зоны') {
    return reduce(
      controlling,
      { type: 'server', msg: { t: 'error', code: 'out_of_range', message, axis } },
      { now: 1_700_000_000_000, cooldownSec: 30 }
    );
  }

  it('текст про границу зоны, а не про предел сустава', () => {
    const p = describeErrorMessage({
      t: 'error',
      code: 'out_of_range',
      message: 'Точка вне рабочей зоны',
      axis: 'x',
    });
    expect(p.title).toBe('Рука у края рабочей зоны');
    expect(p.hint).toContain('вперёд-назад');
    // Серверная диагностика не должна вытеснить объяснение.
    expect(p.preferOwnCopy).toBe(true);
  });

  it('в тост уходит наш текст, а не серверная диагностика', () => {
    const s = jogRefusal('x');
    expect(s.toast?.title).toBe('Рука у края рабочей зоны');
    expect(s.toast?.hint).not.toBe('Точка вне рабочей зоны');
  });

  it('подсвечивается ось, НАЗВАННАЯ сервером, а не та, по которой джогали', () => {
    // Джогали по x, но упёрлись в z: сервер называет z — верим ему.
    const s = jogRefusal('z');
    expect(s.highlight).toEqual({ kind: 'axis', name: 'z' });
  });

  it('ход не теряется и estop не поднимается', () => {
    const s = jogRefusal('y');
    expect(s.phase).toBe('controlling');
    expect(s.estop).toBe(false);
  });

  it('out_of_range по суставу оставляет прежний текст про предел сустава', () => {
    const p = describeErrorMessage({
      t: 'error',
      code: 'out_of_range',
      message: '',
      joint: 'elbow_flex',
    });
    expect(p.title).toBe('Дальше рука не гнётся');
    expect(p.preferOwnCopy).toBeUndefined();
  });
});
