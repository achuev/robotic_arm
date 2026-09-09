import { beforeEach, describe, expect, it } from 'vitest';
import { ManualClock } from './clock';
import {
  ConnectionManager,
  DEFAULT_STABLE_AFTER_MS,
  type SocketLike,
} from './connection';
import type { ClientMessage, ServerMessage } from './protocol';

/** Поддельный сокет: сам ничего не делает, всё дёргаем руками из теста. */
class FakeSocket implements SocketLike {
  onopen: ((ev?: unknown) => void) | null = null;
  onclose: ((ev?: unknown) => void) | null = null;
  onerror: ((ev?: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;

  sent: string[] = [];
  closed = false;

  send(data: string) {
    if (this.closed) throw new Error('socket closed');
    this.sent.push(data);
  }
  close() {
    this.closed = true;
  }

  open() {
    this.onopen?.();
  }
  deliver(msg: ServerMessage | Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  deliverRaw(data: unknown) {
    this.onmessage?.({ data });
  }
  drop() {
    this.closed = true;
    this.onclose?.();
  }
  get messages(): ClientMessage[] {
    return this.sent.map((s) => JSON.parse(s) as ClientMessage);
  }
}

// Троттлинг исходящих: 30 сообщений/с из контракта × бюджет 2/3 = 20 Гц = 50 мс.
// При открытии сокета сразу уходит ping, он занимает слот — поэтому первая
// команда без продвижения часов ещё не улетит.
const THROTTLE_MS = 50;
// Сколько соединение должно продержаться, чтобы счётчик попыток обнулился.
const STABLE_AFTER_MS = DEFAULT_STABLE_AFTER_MS;

function harness(token = 'tok-1') {
  const clock = new ManualClock(1_000_000);
  const sockets: FakeSocket[] = [];
  const cm = new ConnectionManager({
    url: 'ws://test/ws',
    getToken: () => token,
    clock,
    backoff: { jitter: 0 },
    joints: [
      { name: 'elbow_flex', min: -1.69, max: 1.69, label: 'Локоть' },
      { name: 'gripper', min: 0, max: 1, label: 'Захват' },
    ],
    socketFactory: () => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    },
  });
  return { clock, sockets, cm, last: () => sockets[sockets.length - 1] };
}

const STATE: ServerMessage = {
  t: 'state',
  joints: { elbow_flex: 0.2 },
  ee: { x: 0.2, y: 0, z: 0.1 },
  robot: 'idle',
  ts: 1000,
};

describe('ConnectionManager: рукопожатие', () => {
  it('первым сообщением всегда шлёт hello с токеном', () => {
    const { cm, last } = harness('abc-123');
    cm.start();
    last().open();

    const first = last().messages[0];
    expect(first).toEqual({ t: 'hello', token: 'abc-123' });
  });

  it('при переподключении шлёт ТОТ ЖЕ токен — этим и держится ход', () => {
    const { cm, clock, sockets, last } = harness('same-token');
    cm.start();
    last().open();
    last().deliver({ t: 'granted', duration_sec: 90, expires_at: 1090 });
    expect(cm.getState().phase).toBe('controlling');

    last().drop();
    clock.advance(400); // первая попытка через ~300 мс
    expect(sockets.length).toBe(2);
    sockets[1].open();

    expect(sockets[1].messages[0]).toEqual({ t: 'hello', token: 'same-token' });
    // Ход всё ещё наш: сервер даёт 10 секунд на возврат.
    expect(cm.getState().phase).toBe('controlling');
  });
});

describe('ConnectionManager: переподключение', () => {
  it('обрыв на 10 секунд укладывается в несколько попыток', () => {
    const { cm, clock, sockets, last } = harness();
    cm.start();
    last().open();
    last().deliver(STATE);

    // Сервер лежит и отбивает каждое соединение сразу.
    const startedAt = clock.now();
    last().drop();
    while (clock.now() - startedAt < 10_000) {
      clock.advance(100);
      if (!last().closed) last().drop();
    }

    // 300 + 600 + 1200 + 2400 + 4800 = 9300 мс → 5 попыток внутри
    // RECONNECT_GRACE. Если кто-то поднимет baseMs, тест это поймает.
    expect(sockets.length).toBeGreaterThanOrEqual(5);
  });

  it('соединение, которое не открывается и не закрывается, не висит вечно', () => {
    // Классика мобильной сети: captive portal, перескок Wi-Fi → LTE.
    const { cm, clock, sockets, last } = harness();
    cm.start();
    last().open();
    last().deliver(STATE);
    last().drop();

    clock.advance(400); // создан сокет №2, но он молчит
    expect(sockets.length).toBe(2);

    clock.advance(5000); // сработал таймаут подключения
    expect(sockets.length).toBeGreaterThanOrEqual(3);
  });

  it('задержка нарастает, пока сервер не поднимется', () => {
    const { cm, clock, sockets, last } = harness();
    cm.start();
    last().open();

    const delays: number[] = [];
    for (let i = 0; i < 4; i++) {
      last().drop(); // новый сокет тоже отбивается, не открывшись
      delays.push(cm.lastDelayMs);
      clock.advance(cm.lastDelayMs + 1);
    }
    for (let i = 1; i < delays.length; i++) {
      expect(delays[i]).toBeGreaterThan(delays[i - 1]);
    }
    expect(delays).toEqual([300, 600, 1200, 2400]);
    expect(sockets.length).toBe(5);
  });

  it('соединение, продержавшееся достаточно долго, обнуляет счётчик попыток', () => {
    const { cm, clock, last } = harness();
    cm.start();
    last().open();

    last().drop();
    clock.advance(400);
    last().open(); // связь восстановилась
    clock.advance(STABLE_AFTER_MS + 10); // и продержалась
    last().drop();

    expect(cm.lastDelayMs).toBe(300);
  });

  it('мгновенно оборвавшееся соединение счётчик НЕ обнуляет', () => {
    // Защита от «мигания»: по контракту вторая вкладка вытесняет первую, и без
    // этого правила две вкладки выбивали бы друг друга по кругу каждые 300 мс,
    // непрерывно долбя шлюз.
    const { cm, clock, last } = harness();
    cm.start();
    last().open();

    last().drop();
    clock.advance(400);
    last().open();
    last().drop(); // сервер принял и тут же выбросил

    expect(cm.lastDelayMs).toBe(600);
  });

  it('stop прекращает попытки насовсем', () => {
    const { cm, clock, sockets, last } = harness();
    cm.start();
    last().open();
    cm.stop();
    clock.advance(60_000);
    expect(sockets.length).toBe(1);
  });

  it('молчание потока state дольше сторожевого таймера передёргивает сокет', () => {
    const { cm, clock, sockets, last } = harness();
    cm.start();
    last().open();
    last().deliver(STATE);

    // state идёт на 10 Гц; 6 секунд тишины — сокет мёртв.
    clock.advance(6000);
    expect(sockets.length).toBeGreaterThanOrEqual(2);
  });

  it('поток state сторожевой таймер не будит', () => {
    const { cm, clock, sockets, last } = harness();
    cm.start();
    last().open();
    for (let i = 0; i < 100; i++) {
      last().deliver(STATE);
      clock.advance(100); // 10 Гц в течение 10 секунд
    }
    expect(sockets.length).toBe(1);
  });
});

describe('ConnectionManager: разбор входящих', () => {
  it('неизвестный тип сообщения игнорируется, соединение живёт', () => {
    const { cm, sockets, last } = harness();
    cm.start();
    last().open();
    last().deliver({ t: 'quantum_flux', payload: 42 });
    last().deliver(STATE);

    expect(sockets.length).toBe(1);
    expect(cm.getState().phase).toBe('observer');
  });

  it('битый JSON не роняет клиент', () => {
    const { cm, last } = harness();
    cm.start();
    last().open();
    expect(() => last().deliverRaw('{не json')).not.toThrow();
    expect(cm.getState().phase).toBe('connecting');
  });

  it('bad_message заставляет переподключиться', () => {
    const { cm, clock, sockets, last } = harness();
    cm.start();
    last().open();
    last().deliver(STATE);

    last().deliver({ t: 'error', code: 'bad_message', message: 'нет токена' });
    clock.advance(1000);

    expect(sockets.length).toBeGreaterThanOrEqual(2);
    // И это молча: человеку такое показывать незачем.
    expect(cm.getState().toast).toBeNull();
  });

  it('rate_limited притормаживает отправку и ничего не показывает', () => {
    const { cm, last } = harness();
    cm.start();
    last().open();
    const before = cm.outbound.currentIntervalMs;

    last().deliver({ t: 'error', code: 'rate_limited', message: 'too fast' });

    expect(cm.outbound.currentIntervalMs).toBeGreaterThan(before);
    expect(cm.getState().toast).toBeNull();
  });

  it('revoked выбрасывает недоотправленные команды', () => {
    const { cm, clock, last } = harness();
    cm.start();
    last().open();
    last().deliver({ t: 'granted', duration_sec: 90, expires_at: 1090 });

    cm.setJoint('elbow_flex', 0.5);
    clock.advance(THROTTLE_MS); // первая цель успевает уйти
    cm.setJoint('elbow_flex', 0.9); // а эта ещё в очереди
    last().deliver({ t: 'revoked', reason: 'timeout' });
    clock.advance(500);

    const joints = last().messages.filter((m) => m.t === 'set_joint');
    expect(joints).toHaveLength(1);
    expect(cm.getState().phase).toBe('expired');
  });
});

describe('ConnectionManager: исходящие команды', () => {
  let h: ReturnType<typeof harness>;

  beforeEach(() => {
    h = harness();
    h.cm.start();
    h.last().open();
    h.last().deliver({ t: 'granted', duration_sec: 90, expires_at: 1090 });
  });

  it('set_joint уходит абсолютной целью, а не приращением', () => {
    h.cm.setJoint('elbow_flex', 0.42);
    h.clock.advance(THROTTLE_MS);
    const msg = h.last().messages.find((m) => m.t === 'set_joint');
    expect(msg).toEqual({ t: 'set_joint', joint: 'elbow_flex', position: 0.42 });
  });

  it('цель за пределом сустава зажимается ещё на клиенте', () => {
    h.cm.setJoint('elbow_flex', 99);
    h.clock.advance(THROTTLE_MS);
    const msg = h.last().messages.find((m) => m.t === 'set_joint') as {
      position: number;
    };
    expect(msg.position).toBe(1.69);
  });

  it('быстрая протяжка слайдера не превращается в шторм сообщений', () => {
    for (let i = 0; i < 200; i++) {
      h.cm.setJoint('elbow_flex', i / 200);
      h.clock.advance(1);
    }
    h.clock.advance(100);
    const joints = h.last().messages.filter((m) => m.t === 'set_joint');
    // 200 мс протяжки при 20 Гц — единицы сообщений, а не двести.
    expect(joints.length).toBeLessThanOrEqual(8);
  });

  it('enqueue / release / preset уходят немедленно', () => {
    h.cm.enqueue();
    h.cm.preset('wave');
    h.cm.release();
    const types = h.last().messages.map((m) => m.t);
    expect(types).toContain('enqueue');
    expect(types).toContain('preset');
    expect(types).toContain('release');
  });

  it('gripper зажимается в 0..1', () => {
    h.cm.gripper(5);
    h.clock.advance(THROTTLE_MS);
    const msg = h.last().messages.find((m) => m.t === 'gripper') as { value: number };
    expect(msg.value).toBe(1);
  });

  it('пока сокет закрыт, команды не копятся в трубе', () => {
    h.last().drop();
    h.cm.setJoint('elbow_flex', 0.5);
    h.clock.advance(200);
    expect(h.cm.outbound.pendingCount).toBe(0);
  });

  it('heartbeat поддерживает соединение пингом', () => {
    // Держим поток state живым, иначе сработает сторожевой таймер.
    for (let i = 0; i < 32; i++) {
      h.last().deliver(STATE);
      h.clock.advance(500);
    }
    expect(h.last().messages.some((m) => m.t === 'ping')).toBe(true);
  });
});

describe('ConnectionManager: подписка', () => {
  it('уведомляет подписчиков при смене состояния и отписывается', () => {
    const { cm, last } = harness();
    let calls = 0;
    const off = cm.subscribe(() => calls++);
    cm.start();
    last().open();
    last().deliver(STATE);
    expect(calls).toBeGreaterThan(0);

    off();
    const before = calls;
    last().deliver({ t: 'queue', position: 3, ahead: 2, eta_sec: 90, queue_length: 4, you_control: false });
    expect(calls).toBe(before);
  });
});
