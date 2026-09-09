import { Framebuffer, encodePng } from './png.mjs';

/**
 * Рисует руку сбоку по текущим углам суставов. Это не симулятор, а картинка
 * для проверки UI: видно, что поток живой и что он реагирует на команды.
 */

const W = 320;
const H = 240;

const BG = [14, 17, 24];
const GRID = [26, 31, 42];
const TABLE = [40, 46, 60];
const LINK = [226, 232, 240];
const JOINT = [22, 196, 127];
const CLAW = [250, 204, 21];
const SHADOW = [24, 28, 38];

const STATUS_COLOR = {
  idle: [22, 196, 127],
  moving: [250, 204, 21],
  error: [239, 68, 68],
  estop: [239, 68, 68],
};

// Экранные длины звеньев, пиксели.
const L1 = 62;
const L2 = 54;
const L3 = 26;

export function renderFrame(joints, robot, frameIndex) {
  const fb = new Framebuffer(W, H);
  fb.fill(BG);

  for (let x = 0; x < W; x += 20) fb.line(x, 0, x, H, GRID, 1);
  for (let y = 0; y < H; y += 20) fb.line(0, y, W, y, GRID, 1);

  const baseY = H - 42;
  fb.rect(0, baseY + 12, W, H - baseY - 12, TABLE);

  const pan = joints.shoulder_pan ?? 0;
  const lift = joints.shoulder_lift ?? 0;
  const elbow = joints.elbow_flex ?? 0;
  const wrist = joints.wrist_flex ?? 0;
  const grip = joints.gripper ?? 0.5;

  // Поворот основания показываем смещением по горизонтали — вид сбоку-сверху.
  const baseX = W / 2 + Math.sin(pan) * 46;

  // Постамент.
  fb.disc(baseX, baseY + 10, 18, SHADOW);
  fb.rect(baseX - 16, baseY, 32, 14, [58, 64, 81]);

  // Углы на экране: 0 — вертикально вверх, положительные — вправо.
  const a1 = lift;
  const a2 = a1 + elbow;
  const a3 = a2 + wrist;

  const p0 = { x: baseX, y: baseY };
  const p1 = { x: p0.x + Math.sin(a1) * L1, y: p0.y - Math.cos(a1) * L1 };
  const p2 = { x: p1.x + Math.sin(a2) * L2, y: p1.y - Math.cos(a2) * L2 };
  const p3 = { x: p2.x + Math.sin(a3) * L3, y: p2.y - Math.cos(a3) * L3 };

  fb.line(p0.x, p0.y, p1.x, p1.y, LINK, 11);
  fb.line(p1.x, p1.y, p2.x, p2.y, LINK, 9);
  fb.line(p2.x, p2.y, p3.x, p3.y, LINK, 7);

  fb.disc(p0.x, p0.y, 8, JOINT);
  fb.disc(p1.x, p1.y, 7, JOINT);
  fb.disc(p2.x, p2.y, 6, JOINT);

  // Захват: раскрытие 0..1 разводит губки.
  const spread = 4 + grip * 12;
  const nx = Math.cos(a3);
  const ny = Math.sin(a3);
  const tip = { x: p3.x + Math.sin(a3) * 8, y: p3.y - Math.cos(a3) * 8 };
  fb.line(p3.x + nx * spread, p3.y + ny * spread, tip.x + nx * spread, tip.y + ny * spread, CLAW, 5);
  fb.line(p3.x - nx * spread, p3.y - ny * spread, tip.x - nx * spread, tip.y - ny * spread, CLAW, 5);

  // Рамка цветом статуса + бегущая метка, чтобы было видно живой поток.
  const status = STATUS_COLOR[robot] ?? STATUS_COLOR.idle;
  fb.border(3, status);
  const tick = (frameIndex * 4) % (W - 40);
  fb.rect(20 + tick, 8, 16, 4, status);

  return encodePng(fb);
}
