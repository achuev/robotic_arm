import { describe, expect, it } from 'vitest';
import {
  canCommand,
  canEnqueue,
  controlRemainingSec,
  cooldownRemainingSec,
  initialState,
  reduce,
  type ClientEvent,
  type ClientState,
} from './machine';
import type { ServerMessage } from './protocol';

const NOW = 1_700_000_000_000;
const ctx = (now = NOW) => ({ now, cooldownSec: 30 });

function run(events: ClientEvent[], start = initialState, now = NOW): ClientState {
  return events.reduce((s, e) => reduce(s, e, ctx(now)), start);
}

const srv = (msg: ServerMessage): ClientEvent => ({ type: 'server', msg });

const stateMsg = (over: Partial<Extract<ServerMessage, { t: 'state' }>> = {}) =>
  srv({
    t: 'state',
    joints: { shoulder_pan: 0.1, gripper: 0.5 },
    ee: { x: 0.2, y: 0, z: 0.1 },
    robot: 'idle',
    ts: NOW / 1000,
    ...over,
  });

const queueMsg = (over: Partial<Extract<ServerMessage, { t: 'queue' }>> = {}) =>
  srv({
    t: 'queue',
    position: 0,
    ahead: 0,
    eta_sec: 0,
    queue_length: 0,
    you_control: false,
    ...over,
  });

describe('автомат: подключение', () => {
  it('стартует в "connecting" — это короткий скелетон, а не спиннер', () => {
    expect(initialState.phase).toBe('connecting');
    expect(initialState.online).toBe(false);
  });

  it('открытие сокета само по себе не снимает скелетон', () => {
    const s = run([{ type: 'socket_open' }]);
    expect(s.online).toBe(true);
    expect(s.phase).toBe('connecting');
  });

  it('первое же сообщение от сервера переводит в наблюдателя', () => {
    const s = run([{ type: 'socket_open' }, stateMsg()]);
    expect(s.phase).toBe('observer');
    expect(s.joints.shoulder_pan).toBeCloseTo(0.1);
    expect(s.ee).toEqual({ x: 0.2, y: 0, z: 0.1 });
  });
});

describe('автомат: очередь', () => {
  it('position >= 1 → экран очереди', () => {
    const s = run([
      { type: 'socket_open' },
      stateMsg(),
      queueMsg({ position: 3, ahead: 2, eta_sec: 270, queue_length: 4 }),
    ]);
    expect(s.phase).toBe('queued');
    expect(s.queue).toEqual({
      position: 3,
      ahead: 2,
      etaSec: 270,
      queueLength: 4,
    });
  });

  it('you_control = true даёт управление даже без отдельного granted', () => {
    const s = run([
      { type: 'socket_open' },
      queueMsg({ position: 0, you_control: true }),
    ]);
    expect(s.phase).toBe('controlling');
  });

  it('position 0 без you_control — это НЕ управление, а просто наблюдатель', () => {
    const s = run([{ type: 'socket_open' }, queueMsg({ position: 0 })]);
    expect(s.phase).toBe('observer');
  });

  it('нажатие "встать в очередь" гасит кнопку до ответа сервера', () => {
    const s = run([
      { type: 'socket_open' },
      stateMsg(),
      { type: 'enqueue_requested' },
    ]);
    expect(s.pendingEnqueue).toBe(true);
    expect(canEnqueue(s, NOW)).toBe(false);

    const after = reduce(s, queueMsg({ position: 2 }), ctx());
    expect(after.pendingEnqueue).toBe(false);
    expect(after.phase).toBe('queued');
  });
});

describe('автомат: ход', () => {
  it('granted переводит в управление и запоминает дедлайн', () => {
    const s = run([
      { type: 'socket_open' },
      stateMsg(),
      srv({ t: 'granted', duration_sec: 90, expires_at: NOW / 1000 + 90 }),
    ]);
    expect(s.phase).toBe('controlling');
    expect(s.control?.durationSec).toBe(90);
    expect(canCommand(s)).toBe(true);
    expect(controlRemainingSec(s, NOW)).toBe(90);
  });

  it('таймер считает вниз и не уходит в минус', () => {
    const s = run([
      { type: 'socket_open' },
      stateMsg(),
      srv({ t: 'granted', duration_sec: 90, expires_at: NOW / 1000 + 90 }),
    ]);
    expect(controlRemainingSec(s, NOW + 80_000)).toBe(10);
    expect(controlRemainingSec(s, NOW + 200_000)).toBe(0);
  });

  it('revoked ведёт на экран "время вышло" и включает cooldown', () => {
    const granted = run([
      { type: 'socket_open' },
      stateMsg(),
      srv({ t: 'granted', duration_sec: 90, expires_at: NOW / 1000 + 90 }),
    ]);
    const s = reduce(granted, srv({ t: 'revoked', reason: 'timeout' }), ctx());

    expect(s.phase).toBe('expired');
    expect(s.control).toBeNull();
    expect(s.lastRevoke).toBe('timeout');
    expect(cooldownRemainingSec(s, NOW)).toBe(30);
    expect(canEnqueue(s, NOW)).toBe(false);
    expect(canCommand(s)).toBe(false);
  });

  it('через 30 секунд снова можно встать в очередь', () => {
    const s = run([
      { type: 'socket_open' },
      stateMsg(),
      srv({ t: 'granted', duration_sec: 90, expires_at: NOW / 1000 + 90 }),
      srv({ t: 'revoked', reason: 'timeout' }),
    ]);
    expect(canEnqueue(s, NOW + 29_000)).toBe(false);
    expect(canEnqueue(s, NOW + 30_001)).toBe(true);
  });

  it('отзыв админом и estop не наказывают человека cooldown-ом', () => {
    for (const reason of ['admin', 'estop'] as const) {
      const s = run([
        { type: 'socket_open' },
        stateMsg(),
        srv({ t: 'granted', duration_sec: 90, expires_at: NOW / 1000 + 90 }),
        srv({ t: 'revoked', reason }),
      ]);
      expect(s.cooldownUntilMs).toBeNull();
    }
  });

  it('экран "время вышло" не сбивается фоновыми сообщениями очереди', () => {
    const expired = run([
      { type: 'socket_open' },
      stateMsg(),
      srv({ t: 'granted', duration_sec: 90, expires_at: NOW / 1000 + 90 }),
      srv({ t: 'revoked', reason: 'timeout' }),
    ]);
    const s = run([queueMsg({ queue_length: 5 }), stateMsg()], expired);
    expect(s.phase).toBe('expired');

    // ...но человек может уйти с него сам.
    expect(reduce(s, { type: 'watch_again' }, ctx()).phase).toBe('observer');
  });
});

describe('автомат: обрыв связи', () => {
  it('обрыв НЕ отбирает ход — есть RECONNECT_GRACE 10 с', () => {
    const s = run([
      { type: 'socket_open' },
      stateMsg(),
      srv({ t: 'granted', duration_sec: 90, expires_at: NOW / 1000 + 90 }),
      { type: 'socket_closed' },
    ]);
    expect(s.phase).toBe('controlling');
    expect(s.online).toBe(false);
    // Но команды слать некуда, пока сокет закрыт.
    expect(canCommand(s)).toBe(false);
  });

  it('обрыв сохраняет место в очереди на экране до ответа сервера', () => {
    const s = run([
      { type: 'socket_open' },
      stateMsg(),
      queueMsg({ position: 2, ahead: 1 }),
      { type: 'socket_closed' },
    ]);
    expect(s.phase).toBe('queued');
    expect(s.queue?.position).toBe(2);
  });

  it('пока сокет закрыт, встать в очередь нельзя', () => {
    const s = run([{ type: 'socket_open' }, stateMsg(), { type: 'socket_closed' }]);
    expect(canEnqueue(s, NOW)).toBe(false);
  });
});

describe('автомат: аварийная остановка', () => {
  it('robot=estop в потоке state поднимает флаг', () => {
    const s = run([{ type: 'socket_open' }, stateMsg({ robot: 'estop' })]);
    expect(s.estop).toBe(true);
    expect(canEnqueue(s, NOW)).toBe(false);
  });

  it('флаг снимается сам, когда робот ожил', () => {
    const s = run([
      { type: 'socket_open' },
      stateMsg({ robot: 'estop' }),
      stateMsg({ robot: 'idle' }),
    ]);
    expect(s.estop).toBe(false);
  });
});

describe('ход без ограничения по времени', () => {
  it('controlRemainingSec отдаёт null, а не ноль', () => {
    // Ноль и «без ограничения» — разные вещи: ноль зажёг бы «Время
    // заканчивается» человеку, которого никто не торопит.
    const s = run([
      { type: 'socket_open' },
      stateMsg(),
      srv({ t: 'granted', duration_sec: null, expires_at: null }),
    ]);
    expect(s.phase).toBe('controlling');
    expect(controlRemainingSec(s, NOW)).toBeNull();
    expect(controlRemainingSec(s, NOW + 10_000_000)).toBeNull();
  });

  it('срок появляется, когда сервер прислал granted повторно', () => {
    // Так сервер сообщает, что в очередь кто-то встал и отсчёт пошёл.
    const s = run([
      { type: 'socket_open' },
      stateMsg(),
      srv({ t: 'granted', duration_sec: null, expires_at: null }),
      srv({ t: 'granted', duration_sec: 90, expires_at: NOW / 1000 + 90 }),
    ]);
    expect(s.phase).toBe('controlling');
    expect(controlRemainingSec(s, NOW)).toBe(90);
  });

  it('отсутствие хода по-прежнему даёт ноль, а не null', () => {
    expect(controlRemainingSec(run([{ type: 'socket_open' }]), NOW)).toBe(0);
  });
});
