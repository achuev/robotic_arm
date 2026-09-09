import { describe, expect, it } from 'vitest';
import { ManualClock } from './clock';
import { OutboundQueue, intervalForLimit, outboundKey } from './outbound';
import type { ClientMessage, SetJointMsg } from './protocol';

// Интервал по умолчанию — из серверного лимита контракта (30/с × бюджет 2/3).
const BASE_INTERVAL_MS = intervalForLimit(30);

function makeQueue(intervalMs = BASE_INTERVAL_MS) {
  const clock = new ManualClock(1_000_000);
  const sent: ClientMessage[] = [];
  const q = new OutboundQueue({ clock, intervalMs, send: (m) => sent.push(m) });
  return { clock, sent, q };
}

describe('OutboundQueue: троттлинг исходящих', () => {
  it('склеивает поток по ключу и шлёт только последнюю цель', () => {
    const { clock, sent, q } = makeQueue();

    // Палец возит по слайдеру: 100 событий за 10 мс.
    for (let i = 0; i < 100; i++) {
      q.push(outboundKey.joint('elbow_flex'), {
        t: 'set_joint',
        joint: 'elbow_flex',
        position: i / 100,
      });
      clock.advance(0.1);
    }
    clock.advance(200);

    expect(sent.length).toBeLessThanOrEqual(5);
    const last = sent[sent.length - 1] as SetJointMsg;
    expect(last.t).toBe('set_joint');
    expect(last.position).toBeCloseTo(0.99, 5);
  });

  it('первое сообщение уходит сразу, без ожидания интервала', () => {
    const { clock, sent, q } = makeQueue();
    q.push(outboundKey.joint('a'), { t: 'set_joint', joint: 'a', position: 1 });
    clock.advance(0);
    expect(sent).toHaveLength(1);
  });

  it('держит не больше 20 сообщений в секунду', () => {
    const { clock, sent, q } = makeQueue();

    // Непрерывный поток в течение 2 секунд.
    for (let i = 0; i < 2000; i++) {
      q.push(outboundKey.joint('wrist_roll'), {
        t: 'set_joint',
        joint: 'wrist_roll',
        position: i,
      });
      clock.advance(1);
    }

    // 20 Гц * 2 с = 40, плюс допуск на граничную отправку.
    expect(sent.length).toBeLessThanOrEqual(42);
    // Контракт: не больше 30/с. Проверяем средний темп с запасом.
    expect(sent.length / 2).toBeLessThan(30);
  });

  it('не теряет разные ключи: каждая ось джоггинга доезжает', () => {
    const { clock, sent, q } = makeQueue();
    q.push(outboundKey.jog('x'), { t: 'jog_ee', axis: 'x', delta: 0.01 });
    q.push(outboundKey.jog('y'), { t: 'jog_ee', axis: 'y', delta: -0.01 });
    clock.advance(200);

    const axes = sent.map((m) => (m as { axis?: string }).axis);
    expect(axes).toContain('x');
    expect(axes).toContain('y');
  });

  it('sendImmediate не ждёт очередь', () => {
    const { sent, q } = makeQueue();
    q.push(outboundKey.joint('a'), { t: 'set_joint', joint: 'a', position: 1 });
    q.push(outboundKey.joint('b'), { t: 'set_joint', joint: 'b', position: 1 });
    q.sendImmediate({ t: 'release' });
    expect(sent.some((m) => m.t === 'release')).toBe(true);
  });

  it('rate_limited увеличивает интервал, а потом отпускает обратно', () => {
    const { clock, q } = makeQueue();
    const base = q.currentIntervalMs;

    q.noteRateLimited();
    expect(q.currentIntervalMs).toBeGreaterThan(base);
    const backedOff = q.currentIntervalMs;

    q.noteRateLimited();
    expect(q.currentIntervalMs).toBeGreaterThan(backedOff);

    // Прошло спокойное время — темп восстанавливается.
    clock.advance(4000);
    for (let i = 0; i < 40; i++) {
      q.push(outboundKey.joint('a'), { t: 'set_joint', joint: 'a', position: i });
      clock.advance(300);
    }
    expect(q.currentIntervalMs).toBeCloseTo(base, 5);
  });

  it('интервал под нагрузкой не превышает потолок', () => {
    const { q } = makeQueue();
    for (let i = 0; i < 50; i++) q.noteRateLimited();
    expect(q.currentIntervalMs).toBeLessThanOrEqual(250);
  });

  it('clear выбрасывает неотправленное', () => {
    const { clock, sent, q } = makeQueue();
    q.push(outboundKey.joint('a'), { t: 'set_joint', joint: 'a', position: 1 });
    clock.advance(0); // первое ушло
    q.push(outboundKey.joint('b'), { t: 'set_joint', joint: 'b', position: 2 });
    q.clear();
    clock.advance(500);
    expect(sent).toHaveLength(1);
  });

  it('pause глушит очередь, resume возвращает мгновенную отправку', () => {
    const { clock, sent, q } = makeQueue();
    q.pause();
    q.push(outboundKey.joint('a'), { t: 'set_joint', joint: 'a', position: 1 });
    clock.advance(500);
    expect(sent).toHaveLength(0);

    q.resume();
    q.push(outboundKey.joint('a'), { t: 'set_joint', joint: 'a', position: 2 });
    clock.advance(0);
    expect(sent).toHaveLength(1);
  });
});
