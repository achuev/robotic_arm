import type { Workspace } from './protocol';

/**
 * Рабочая зона схвата: где мы относительно её границ.
 *
 * Зачем это нужно отдельным модулем. На свежезапущенном стенде рука стоит в
 * нулевой позе — вытянутая вперёд, — и эта поза ЛЕЖИТ ВНЕ объявленной
 * `workspace` (на живом стенде x = 0.391 при границе 0.38). Первый же джог
 * возвращает `out_of_range`, хотя человек не сделал ничего неправильного:
 * контракт §3 говорит, что значение клэмпится, а движение всё равно
 * исполняется — то есть рука как раз возвращается в зону.
 *
 * Показывать это как ошибку — врать. Поэтому интерфейс считает границы сам,
 * по `state.ee` и `config.workspace`, и объясняет положение ЗАРАНЕЕ, не
 * дожидаясь отказа сервера.
 */

export type Axis = 'x' | 'y' | 'z';

export const AXES: readonly Axis[] = ['x', 'y', 'z'];

export interface Point {
  x: number;
  y: number;
  z: number;
}

/** Сторона границы: -1 — нижняя (min), +1 — верхняя (max). */
export type Side = -1 | 1;

export interface AxisEdge {
  axis: Axis;
  side: Side;
  /**
   * На сколько метров точка вышла ЗА границу. Ноль — стоим на границе
   * (в пределах допуска), больше нуля — уже снаружи.
   */
  overshootM: number;
}

/**
 * Насколько близко к границе считается «у края», метры.
 * Шаг джоггинга по умолчанию — 0.01 м, так что 5 мм — это «следующий шаг
 * упрётся», а не «когда-нибудь потом».
 */
export const EDGE_TOLERANCE_M = 0.005;

/** Куда двигает пад по каждому направлению — те же слова, что на джойстике. */
export const DIRECTION_LABEL: Record<string, string> = {
  'x+': 'вперёд',
  'x-': 'назад',
  'y+': 'вправо',
  'y-': 'влево',
  'z+': 'выше',
  'z-': 'ниже',
};

/** Как назвать ось целиком, когда сторона неизвестна (её сервер не сообщает). */
export const AXIS_LABEL: Record<Axis, string> = {
  x: 'вперёд-назад',
  y: 'влево-вправо',
  z: 'вверх-вниз',
};

export function directionKey(axis: Axis, side: Side): string {
  return `${axis}${side > 0 ? '+' : '-'}`;
}

/** Ось за/на границе — или null, если запас есть. */
export function axisEdge(
  axis: Axis,
  value: number,
  bounds: readonly [number, number] | undefined,
  tolerance = EDGE_TOLERANCE_M
): AxisEdge | null {
  if (!bounds) return null;
  const [min, max] = bounds;
  if (!Number.isFinite(value) || !Number.isFinite(min) || !Number.isFinite(max)) {
    return null;
  }
  // Вырожденная зона (шлюз не отдал workspace) — молчим, а не пугаем.
  if (max <= min) return null;

  if (value >= max - tolerance) {
    return { axis, side: 1, overshootM: Math.max(0, value - max) };
  }
  if (value <= min + tolerance) {
    return { axis, side: -1, overshootM: Math.max(0, min - value) };
  }
  return null;
}

/**
 * Все оси, по которым схват стоит на границе зоны или уже вышел за неё.
 * Порядок стабильный: x, y, z — чтобы текст не прыгал между кадрами.
 */
export function workspaceEdges(
  ee: Point | null | undefined,
  workspace: Workspace | null | undefined,
  tolerance = EDGE_TOLERANCE_M
): AxisEdge[] {
  if (!ee || !workspace) return [];
  const out: AxisEdge[] = [];
  for (const axis of AXES) {
    const edge = axisEdge(axis, ee[axis], workspace[axis], tolerance);
    if (edge) out.push(edge);
  }
  return out;
}

/** Точка вне коробки — строго, без допуска. */
export function isOutsideWorkspace(
  ee: Point | null | undefined,
  workspace: Workspace | null | undefined
): boolean {
  return workspaceEdges(ee, workspace, 0).some((e) => e.overshootM > 0);
}

/** Набор заблокированных направлений: ключи вида `x+`, `z-`. */
export function blockedDirections(edges: AxisEdge[]): Set<string> {
  return new Set(edges.map((e) => directionKey(e.axis, e.side)));
}

export type WorkspaceNoticeKind = 'outside' | 'edge';

export interface WorkspaceNotice {
  kind: WorkspaceNoticeKind;
  title: string;
  hint: string;
  edges: AxisEdge[];
}

/**
 * Спокойный текст про положение руки относительно зоны.
 *
 * `outside` — рука ЗА границей (обычно это стартовая поза стенда);
 * `edge` — рука на границе, следующий шаг в эту сторону не пройдёт.
 * И то и другое — нормальная работа, а не поломка, поэтому и слова
 * подобраны так, чтобы человек не решил, что он что-то сломал.
 */
export function describeWorkspacePosition(
  ee: Point | null | undefined,
  workspace: Workspace | null | undefined,
  tolerance = EDGE_TOLERANCE_M
): WorkspaceNotice | null {
  const edges = workspaceEdges(ee, workspace, tolerance);
  if (edges.length === 0) return null;

  const outside = edges.filter((e) => e.overshootM > 0);
  if (outside.length > 0) {
    return {
      kind: 'outside',
      title: 'Рука у самого края зоны',
      hint: `Это не поломка — так бывает сразу после включения стенда. Движение ${listDirections(
        outside.map((e) => directionKey(e.axis, negate(e.side)))
      )} вернёт руку внутрь.`,
      edges,
    };
  }

  return {
    kind: 'edge',
    title: 'Рука у края рабочей зоны',
    hint: `Дальше ${listDirections(
      edges.map((e) => directionKey(e.axis, e.side))
    )} не поедет. Остальные направления работают.`,
    edges,
  };
}

/** Текст для тоста `out_of_range` с осью: сервер называет ось, но не сторону. */
export function axisEdgeHint(axis: string | undefined): string {
  const label = AXIS_LABEL[axis as Axis];
  return label
    ? `Дальше ${label} рука не поедет — она уже на границе зоны. Попробуйте другое направление.`
    : 'Рука дошла до границы рабочей зоны. Попробуйте другое направление.';
}

function negate(side: Side): Side {
  return side > 0 ? -1 : 1;
}

/** «вперёд», «вперёд и выше», «вперёд, вправо и выше». */
function listDirections(keys: string[]): string {
  const words = keys.map((k) => DIRECTION_LABEL[k] ?? k);
  if (words.length === 0) return '';
  if (words.length === 1) return words[0];
  return `${words.slice(0, -1).join(', ')} и ${words[words.length - 1]}`;
}
