/**
 * Модель руки для мок-сервера: пределы, ограничитель скорости, прямая и
 * обратная кинематика. Ровно настолько подробная, чтобы UI вёл себя как с
 * настоящим железом — цель ставится мгновенно, а сустав доезжает плавно.
 */

export const JOINTS = [
  { name: 'shoulder_pan', min: -1.92, max: 1.92, label: 'Основание', speed: 1.6 },
  { name: 'shoulder_lift', min: -1.75, max: 1.75, label: 'Плечо', speed: 1.4 },
  { name: 'elbow_flex', min: -1.69, max: 1.69, label: 'Локоть', speed: 1.6 },
  { name: 'wrist_flex', min: -1.66, max: 1.66, label: 'Кисть', speed: 2.0 },
  { name: 'wrist_roll', min: -2.79, max: 2.79, label: 'Поворот кисти', speed: 2.4 },
  // Захват — единственный сустав в долях 0..1, а не в радианах (контракт §2).
  { name: 'gripper', min: 0.0, max: 1.0, label: 'Захват', speed: 2.5, normalized: true },
];

export const WORKSPACE = { x: [0.05, 0.35], y: [-0.25, 0.25], z: [0.02, 0.35] };

// Длины звеньев, метры. Сумма 0.36 — покрывает рабочую зону из конфига.
const L1 = 0.18;
const L2 = 0.18;
const BASE_H = 0.06;

/**
 * Домашняя поза. Схват в ней обязан лежать ВНУТРИ объявленной `workspace`:
 * рука уезжает сюда на каждой передаче хода, и если бы home оказался снаружи,
 * плашка «рука у края зоны» горела бы постоянно и перестала что-либо значить.
 * FK этой позы — (0.125, 0, 0.318), с запасом внутри зоны.
 */
export const HOME = {
  shoulder_pan: 0,
  shoulder_lift: 1.1,
  elbow_flex: -1.3,
  wrist_flex: 0.2,
  wrist_roll: 0,
  gripper: 0.5,
};

/**
 * Нулевая поза: рука вытянута, все углы по нулям.
 *
 * Именно так стенд встречает утро — и эта поза ЛЕЖИТ ВНЕ объявленной
 * `workspace` (на живом железе x = 0.391 при границе 0.38; здесь по той же
 * причине вылезает z). Мок обязан уметь её воспроизвести, иначе экран
 * «рука у края зоны» нечем проверить: `MOCK_ZERO_POSE=1` или `/mock/zero`.
 */
export const ZERO_POSE = {
  shoulder_pan: 0,
  shoulder_lift: 0,
  elbow_flex: 0,
  wrist_flex: 0,
  wrist_roll: 0,
  gripper: 0.5,
};

export function clamp(v, min, max) {
  return Number.isFinite(v) ? Math.min(max, Math.max(min, v)) : min;
}

export function jointSpec(name) {
  return JOINTS.find((j) => j.name === name);
}

export class RobotModel {
  constructor() {
    this.actual = { ...HOME };
    this.target = { ...HOME };
  }

  /** @returns {boolean} движется ли рука прямо сейчас */
  step(dtSec) {
    let moving = false;
    for (const spec of JOINTS) {
      const cur = this.actual[spec.name];
      const goal = this.target[spec.name];
      const diff = goal - cur;
      const maxStep = spec.speed * dtSec;
      if (Math.abs(diff) <= maxStep) {
        this.actual[spec.name] = goal;
      } else {
        this.actual[spec.name] = cur + Math.sign(diff) * maxStep;
        moving = true;
      }
    }
    return moving;
  }

  /**
   * Ставит абсолютную цель.
   * @returns {'ok'|'out_of_range'|'unknown_joint'}
   */
  setTarget(name, position) {
    const spec = jointSpec(name);
    if (!spec) return 'unknown_joint';
    const clamped = clamp(position, spec.min, spec.max);
    this.target[name] = clamped;
    return clamped !== position ? 'out_of_range' : 'ok';
  }

  goHome() {
    this.target = { ...HOME };
  }

  /** Мгновенно поставить руку в позу — для воспроизведения стартового состояния. */
  snapTo(pose) {
    this.actual = { ...pose };
    this.target = { ...pose };
  }

  /** Прямая кинематика: положение схвата по текущим углам. */
  ee() {
    const pan = this.actual.shoulder_pan;
    const a1 = this.actual.shoulder_lift;
    const a2 = a1 + this.actual.elbow_flex;
    const r = Math.sin(a1) * L1 + Math.sin(a2) * L2;
    const z = BASE_H + Math.cos(a1) * L1 + Math.cos(a2) * L2;
    return {
      x: Math.cos(pan) * r,
      y: Math.sin(pan) * r,
      z,
    };
  }

  /** Цель схвата по целевым (а не фактическим) углам — от неё считается джоггинг. */
  targetEe() {
    const pan = this.target.shoulder_pan;
    const a1 = this.target.shoulder_lift;
    const a2 = a1 + this.target.elbow_flex;
    const r = Math.sin(a1) * L1 + Math.sin(a2) * L2;
    const z = BASE_H + Math.cos(a1) * L1 + Math.cos(a2) * L2;
    return { x: Math.cos(pan) * r, y: Math.sin(pan) * r, z };
  }

  /**
   * Обратная кинематика в целевые углы.
   *
   * Точка ПОДРЕЗАЕТСЯ в рабочую зону и движение всё равно исполняется —
   * контракт §3: «значение клэмпится, движение не отменяется». Иначе рука,
   * включившаяся вне зоны (нулевая поза), не смогла бы в неё вернуться:
   * каждый джог отказывал бы, и стенд выглядел бы сломанным.
   *
   * `axis` — та ось, которая ДЕЙСТВИТЕЛЬНО упёрлась (с наибольшим выходом за
   * границу), а не та, по которой джогали: диагональный шаг упирается в другую.
   *
   * @returns {{status: 'ok'|'out_of_range'|'ik_failed', axis?: 'x'|'y'|'z'}}
   */
  moveEeTo({ x, y, z }) {
    // ПОРЯДОК ВАЖЕН: сначала границы зоны, потом IK — контракт §3.
    const point = { x, y, z };
    const clamped = {};
    let worstAxis = null;
    let worstOvershoot = 0;
    for (const axis of ['x', 'y', 'z']) {
      const [min, max] = WORKSPACE[axis];
      clamped[axis] = clamp(point[axis], min, max);
      const overshoot = Math.abs(point[axis] - clamped[axis]);
      if (overshoot > worstOvershoot) {
        worstOvershoot = overshoot;
        worstAxis = axis;
      }
    }
    const outOfZone = worstAxis !== null;

    const pan = Math.atan2(clamped.y, clamped.x);
    const r = Math.hypot(clamped.x, clamped.y);
    const dz = clamped.z - BASE_H;
    const d = Math.hypot(r, dz);

    const fail = () =>
      // Вне зоны — сообщаем именно об этом, даже если подрезанная точка вдобавок
      // недостижима: ik_failed остаётся для точек ВНУТРИ зоны.
      outOfZone
        ? { status: 'out_of_range', axis: worstAxis }
        : { status: 'ik_failed' };

    if (d > L1 + L2 || d < Math.abs(L1 - L2) + 1e-6) return fail();

    const cosElbow = (d * d - L1 * L1 - L2 * L2) / (2 * L1 * L2);
    if (cosElbow < -1 || cosElbow > 1) return fail();
    const elbow = -Math.acos(cosElbow);
    const lift =
      Math.atan2(r, dz) - Math.atan2(L2 * Math.sin(elbow), L1 + L2 * Math.cos(elbow));

    const panSpec = jointSpec('shoulder_pan');
    const liftSpec = jointSpec('shoulder_lift');
    const elbowSpec = jointSpec('elbow_flex');
    if (
      pan < panSpec.min || pan > panSpec.max ||
      lift < liftSpec.min || lift > liftSpec.max ||
      elbow < elbowSpec.min || elbow > elbowSpec.max
    ) {
      return fail();
    }

    this.target.shoulder_pan = pan;
    this.target.shoulder_lift = lift;
    this.target.elbow_flex = elbow;
    // Кисть держим горизонтально, чтобы схват не смотрел в потолок.
    this.target.wrist_flex = clamp(
      -(lift + elbow),
      jointSpec('wrist_flex').min,
      jointSpec('wrist_flex').max
    );
    // Движение исполнено; про подрезку клиент всё равно узнаёт.
    return outOfZone ? { status: 'out_of_range', axis: worstAxis } : { status: 'ok' };
  }
}

/* ------------------------------------------------------------------ пресеты */

/** «Помахать»: сценарий из кадров {time_s, joints}. */
export const WAVE_SCRIPT = [
  { at: 0.0, joints: { shoulder_lift: 0.2, elbow_flex: -1.2, wrist_flex: 0.4, gripper: 1 } },
  { at: 0.7, joints: { wrist_roll: 0.9 } },
  { at: 1.3, joints: { wrist_roll: -0.9 } },
  { at: 1.9, joints: { wrist_roll: 0.9 } },
  { at: 2.5, joints: { wrist_roll: -0.9 } },
  { at: 3.1, joints: { wrist_roll: 0 } },
];
