/**
 * Проверка, что 3D-вид собирает руку так же, как её видит ROS.
 *
 * Эталон в `armChain.fixtures.json` снят с ЖИВОГО стенда: шесть поз, для
 * каждой — фактические углы суставов и положение схвата, которое шлюз взял
 * из TF (`base_link` → `gripper_frame_link`). Это независимый источник:
 * там честная цепочка `robot_state_publisher`, а здесь — наша реализация
 * по данным из URDF. Расхождение означает ошибку сборки цепочки.
 *
 * Такая ошибка иначе не ловится: рука на экране выглядит правдоподобно,
 * просто схват оказывается не там, где на самом деле.
 *
 * Пересобрать эталон (нужен поднятый стенд):
 *   gateway/.venv/bin/python tools/capture_fk_fixtures.py
 */

import { describe, expect, it } from 'vitest';

import { JOINTS, VISUALS, ROOT_LINK, MATERIALS } from './armChain';
import fixtures from './armChain.fixtures.json';
import {
  axisRotation,
  endEffector,
  jointAngle,
  linkTransforms,
  multiply,
  originMatrix,
  translationOf,
  visualTransforms,
  IDENTITY,
} from './fk';

/** Допуск сверки со стендом, метры. */
const TOL_M = 5e-4; // 0.5 мм

describe('цепочка из URDF', () => {
  it('содержит шесть подвижных суставов и фиксированный схват', () => {
    const revolute = JOINTS.filter((j) => j.type === 'revolute').map((j) => j.name);
    expect(revolute.sort()).toEqual([
      'elbow_flex', 'gripper', 'shoulder_lift', 'shoulder_pan', 'wrist_flex', 'wrist_roll',
    ]);
    expect(JOINTS.find((j) => j.name === 'gripper_frame_joint')?.type).toBe('fixed');
  });

  it('связна: у каждого сустава родитель либо корень, либо чей-то потомок', () => {
    const children = new Set(JOINTS.map((j) => j.child));
    for (const joint of JOINTS) {
      expect(joint.parent === ROOT_LINK || children.has(joint.parent)).toBe(true);
    }
  });

  it('каждый визуал привязан к достижимому звену и имеет материал', () => {
    const reachable = linkTransforms({});
    for (const visual of VISUALS) {
      expect(reachable.has(visual.link)).toBe(true);
      expect(MATERIALS[visual.material]).toBeDefined();
    }
  });

  it('у звеньев по нескольку мешей — их нельзя ключевать по имени звена', () => {
    // У base_link четыре визуала. Отображение «звено → меш» потеряло бы три
    // из них, и рука осталась бы без корпуса. Ровно так и вышло в первой
    // версии: собиралась из одних сервоприводов, висящих в воздухе.
    expect(VISUALS.length).toBeGreaterThan(new Set(VISUALS.map((v) => v.link)).size);
  });
});

describe('матричная арифметика', () => {
  it('нулевой origin даёт единичную матрицу', () => {
    // Поэлементно, а не toEqual: -sin(0) даёт -0, и строгое сравнение
    // отличило бы его от 0, хотя матрица единичная.
    originMatrix([0, 0, 0], [0, 0, 0]).forEach((v, i) => expect(v).toBeCloseTo(IDENTITY[i], 15));
  });

  it('rpy применяется в порядке URDF: Rz·Ry·Rx', () => {
    const roll = originMatrix([0, 0, 0], [0.3, 0, 0]);
    const pitch = originMatrix([0, 0, 0], [0, 0.4, 0]);
    const yaw = originMatrix([0, 0, 0], [0, 0, 0.5]);
    const combined = originMatrix([0, 0, 0], [0.3, 0.4, 0.5]);
    const expected = multiply(yaw, multiply(pitch, roll));
    combined.forEach((v, i) => expect(v).toBeCloseTo(expected[i], 12));
  });

  it('поворот вокруг Z на 90° переводит X в Y', () => {
    const m = axisRotation([0, 0, 1], Math.PI / 2);
    const p = multiply(m, originMatrix([1, 0, 0], [0, 0, 0]));
    const [x, y, z] = translationOf(p);
    expect(x).toBeCloseTo(0, 12);
    expect(y).toBeCloseTo(1, 12);
    expect(z).toBeCloseTo(0, 12);
  });

  it('перенос переносит, а не поворачивает', () => {
    expect(translationOf(originMatrix([0.1, -0.2, 0.3], [0, 0, 0]))).toEqual([0.1, -0.2, 0.3]);
  });
});

describe('нормировка захвата', () => {
  const gripper = JOINTS.find((j) => j.name === 'gripper')!;

  it('0 и 1 отображаются в пределы сустава из URDF', () => {
    expect(jointAngle(gripper, 0)).toBeCloseTo(gripper.limit![0], 12);
    expect(jointAngle(gripper, 1)).toBeCloseTo(gripper.limit![1], 12);
  });

  it('значение из home (0.09091) даёт примерно ноль радиан', () => {
    // Ровно так стенд и отдаёт закрытый схват в домашней позе.
    expect(jointAngle(gripper, 0.09091)).toBeCloseTo(0, 4);
  });

  it('выход за 0..1 зажимается, а не выворачивает схват', () => {
    expect(jointAngle(gripper, 5)).toBeCloseTo(gripper.limit![1], 12);
    expect(jointAngle(gripper, -3)).toBeCloseTo(gripper.limit![0], 12);
  });

  it('остальные суставы проходят насквозь, в радианах', () => {
    const elbow = JOINTS.find((j) => j.name === 'elbow_flex')!;
    expect(jointAngle(elbow, 0.7)).toBe(0.7);
  });
});

describe('сверка со стендом: схват должен попасть туда же, куда TF', () => {
  for (const f of fixtures) {
    it(`поза «${f.label}»`, () => {
      const got = endEffector(f.joints as Record<string, number>);
      expect(got).not.toBeNull();
      const [x, y, z] = got!;
      expect(x).toBeCloseTo(f.ee.x, 3);
      expect(y).toBeCloseTo(f.ee.y, 3);
      expect(z).toBeCloseTo(f.ee.z, 3);
      // toBeCloseTo(…, 3) — это 0.5 мм; заодно проверяем явно, чтобы
      // при провале в отчёте была видна величина расхождения.
      const err = Math.hypot(x - f.ee.x, y - f.ee.y, z - f.ee.z);
      expect(err).toBeLessThan(TOL_M);
    });
  }

  it('поза влияет на результат (тест не сравнивает константы)', () => {
    const a = endEffector(fixtures[0].joints as Record<string, number>)!;
    const b = endEffector(fixtures[2].joints as Record<string, number>)!;
    expect(Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])).toBeGreaterThan(0.05);
  });

  it('отсутствующий угол считается нулём, а не ломает цепочку', () => {
    const got = endEffector({ shoulder_pan: 0.2 });
    expect(got).not.toBeNull();
    expect(got!.every(Number.isFinite)).toBe(true);
  });
});

describe('визуалы', () => {
  it('преобразование есть у каждого визуала, и порядок совпадает с VISUALS', () => {
    const vis = visualTransforms({ shoulder_pan: 0.3, elbow_flex: -0.4 });
    expect(vis.length).toBe(VISUALS.length);
    for (const m of vis) expect(m.every(Number.isFinite)).toBe(true);
  });

  it('смещение визуала учтено: оно не совпадает с положением звена', () => {
    // У upper_arm_link сервопривод смещён на -0.11257 по X. Если забыть
    // смещение, меш сядет в шарнир, и рука соберётся «внахлёст».
    const links = linkTransforms({});
    const vis = visualTransforms({});
    const idx = VISUALS.findIndex((v) => v.link === 'upper_arm_link');
    const a = translationOf(links.get('upper_arm_link')!);
    const b = translationOf(vis[idx]);
    expect(Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])).toBeGreaterThan(0.1);
  });

  it('рука собрана, а не рассыпана: соседние детали рядом', () => {
    // Каждая деталь обязана иметь соседа ближе 12 см. Разлетевшаяся модель
    // (первая версия) этого не проходит: куски стояли в 20+ см друг от друга.
    const pts = visualTransforms({}).map(translationOf);
    for (let i = 0; i < pts.length; i++) {
      const nearest = Math.min(
        ...pts.filter((_, j) => j !== i)
          .map((q) => Math.hypot(pts[i][0] - q[0], pts[i][1] - q[1], pts[i][2] - q[2])),
      );
      expect(nearest, `визуал ${VISUALS[i].mesh} стоит на отшибе`).toBeLessThan(0.12);
    }
  });
});
