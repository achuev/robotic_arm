/**
 * Прямая кинематика руки по цепочке из URDF.
 *
 * Считает положение каждого звена по углам суставов — тем самым, что приходят
 * в `state.joints` десять раз в секунду. Нужна для 3D-вида: сцена собирается
 * из тех же преобразований, по которым `robot_state_publisher` строит TF.
 *
 * Модуль сознательно НЕ зависит от three.js: это чистые матрицы, поэтому его
 * можно прогнать в node и сверить с эталоном (`armChain.test.ts`) — схват,
 * посчитанный здесь, обязан сойтись с полем `ee`, которое сервер получает
 * из TF независимо от нас.
 *
 * Матрицы — 4×4 в СТРОЧНОМ порядке (row-major), 16 чисел. Так их принимает
 * `THREE.Matrix4.set(...)`, и лишнего транспонирования нигде не возникает.
 */

import { JOINTS, VISUALS, ROOT_LINK, type ChainJoint, type Vec3 } from './armChain';

export type Mat4 = number[];

export const IDENTITY: Mat4 = [
  1, 0, 0, 0,
  0, 1, 0, 0,
  0, 0, 1, 0,
  0, 0, 0, 1,
];

/**
 * Преобразование из `<origin xyz="..." rpy="..."/>`.
 *
 * URDF задаёт поворот как rpy вокруг НЕПОДВИЖНЫХ осей, что эквивалентно
 * произведению Rz(yaw)·Ry(pitch)·Rx(roll) — именно в этом порядке. Перепутать
 * порядок здесь означает собрать руку правильных размеров, но вывернутую.
 */
export function originMatrix(xyz: Vec3, rpy: Vec3): Mat4 {
  const [x, y, z] = xyz;
  const [r, p, yw] = rpy;
  const cr = Math.cos(r), sr = Math.sin(r);
  const cp = Math.cos(p), sp = Math.sin(p);
  const cy = Math.cos(yw), sy = Math.sin(yw);

  return [
    cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, x,
    sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, y,
    -sp,     cp * sr,                cp * cr,                z,
    0,       0,                      0,                      1,
  ];
}

/** Поворот на `angle` вокруг единичной оси — формула Родрига. */
export function axisRotation(axis: Vec3, angle: number): Mat4 {
  const n = Math.hypot(axis[0], axis[1], axis[2]) || 1;
  const [x, y, z] = [axis[0] / n, axis[1] / n, axis[2] / n];
  const c = Math.cos(angle), s = Math.sin(angle), t = 1 - c;

  return [
    t * x * x + c,     t * x * y - s * z, t * x * z + s * y, 0,
    t * x * y + s * z, t * y * y + c,     t * y * z - s * x, 0,
    t * x * z - s * y, t * y * z + s * x, t * z * z + c,     0,
    0,                 0,                 0,                 1,
  ];
}

export function multiply(a: Mat4, b: Mat4): Mat4 {
  const out = new Array<number>(16);
  for (let row = 0; row < 4; row++) {
    for (let col = 0; col < 4; col++) {
      let sum = 0;
      for (let k = 0; k < 4; k++) sum += a[row * 4 + k] * b[k * 4 + col];
      out[row * 4 + col] = sum;
    }
  }
  return out;
}

export function translationOf(m: Mat4): Vec3 {
  return [m[3], m[7], m[11]];
}

/**
 * Захват приходит с сервера НОРМИРОВАННЫМ 0..1 (см. docs/api.md), а не в
 * радианах: наружу отдаётся «насколько открыт», а не угол сервопривода.
 * Модели нужен угол, поэтому разворачиваем по пределам сустава из URDF.
 *
 * Остальные суставы приходят в радианах и проходят насквозь.
 */
export function jointAngle(joint: ChainJoint, value: number): number {
  if (joint.name !== 'gripper' || !joint.limit) return value;
  const [lo, hi] = joint.limit;
  return lo + clamp01(value) * (hi - lo);
}

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

const JOINTS_BY_PARENT = new Map<string, ChainJoint[]>();
for (const joint of JOINTS) {
  const list = JOINTS_BY_PARENT.get(joint.parent);
  if (list) list.push(joint);
  else JOINTS_BY_PARENT.set(joint.parent, [joint]);
}

/**
 * Положение всех звеньев относительно `base_link`.
 *
 * Обход именно по дереву от корня, а не по порядку массива: в URDF суставы
 * лежат вперемешку (`gripper_frame_joint` стоит раньше `wrist_roll`, хотя
 * зависит от него), и последовательный проход собрал бы руку неправильно.
 */
export function linkTransforms(values: Record<string, number>): Map<string, Mat4> {
  const out = new Map<string, Mat4>([[ROOT_LINK, IDENTITY]]);
  const queue: string[] = [ROOT_LINK];

  while (queue.length) {
    const parent = queue.shift()!;
    const parentTf = out.get(parent)!;

    for (const joint of JOINTS_BY_PARENT.get(parent) ?? []) {
      const origin = originMatrix(joint.xyz, joint.rpy);
      let local = origin;

      if (joint.type === 'revolute') {
        const raw = values[joint.name];
        const angle = Number.isFinite(raw) ? jointAngle(joint, raw) : 0;
        local = multiply(origin, axisRotation(joint.axis, angle));
      }

      out.set(joint.child, multiply(parentTf, local));
      queue.push(joint.child);
    }
  }
  return out;
}

/** Положение схвата — то же, что сервер шлёт в `state.ee`. */
export function endEffector(values: Record<string, number>): Vec3 | null {
  const tf = linkTransforms(values).get('gripper_frame_link');
  return tf ? translationOf(tf) : null;
}

/**
 * Преобразования всех визуалов: положение звена плюс смещение меша.
 *
 * Возвращает массив, выровненный по индексам `VISUALS`, а не отображение по
 * имени звена: у одного звена бывает несколько мешей (у `base_link` — четыре),
 * и ключ по имени молча оставил бы от каждого звена по одному.
 */
export function visualTransforms(values: Record<string, number>): Mat4[] {
  const links = linkTransforms(values);
  return VISUALS.map((v) => {
    const base = links.get(v.link);
    return base ? multiply(base, originMatrix(v.xyz, v.rpy)) : IDENTITY;
  });
}
