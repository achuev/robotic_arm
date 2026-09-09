import { describe, expect, it } from 'vitest';
import {
  AdminSession,
  DEFAULT_CONFIRM_TIMEOUT_MS,
  controllerIdleSec,
  controllerRemainingSec,
  isSnapshotStale,
  memoryTokenStorage,
  needsConfirm,
  parseAdminSnapshot,
  participants,
  shortToken,
  type AdminFetch,
  type AdminFetchInit,
  type AdminState,
} from './admin';
import { ManualClock } from './clock';

/**
 * Стенд для админки: ручные часы и поддельный fetch — ровно тот же приём,
 * что и в connection.test.ts. Реального времени и реальной сети здесь нет.
 */

const TOKEN = 'super-secret-admin';
const POLL_MS = 1000;
const REQUEST_TIMEOUT_MS = 4000;

interface Call {
  url: string;
  init: AdminFetchInit;
}

/** Ответ шлюза на `GET /api/admin/queue` в форме so101_gateway. */
function snapshotBody(over: Record<string, unknown> = {}) {
  return {
    now: 1_700_000_000,
    robot: 'idle',
    estop: false,
    handover_until: null,
    controller: {
      token: '3f2504e0-4f89-11d3-9a0c-0305e82c3301',
      connected: true,
      expires_at: 1_700_000_060,
      remaining_sec: 60,
      idle_for_sec: 3,
      disconnected_for_sec: null,
    },
    queue: [
      { token: 'aaaaaaaa-0000', position: 1, eta_sec: 92, connected: true },
      { token: 'bbbbbbbb-0000', position: 2, eta_sec: 184, connected: false },
    ],
    observers: ['cccccccc-0000'],
    cooldowns: { 'dddddddd-0000': 12.5 },
    ...over,
  };
}

class Gateway {
  calls: Call[] = [];
  /** Что вернуть на следующий запрос очереди. */
  body: unknown = snapshotBody();
  status = 200;
  /** Ронять fetch, изображая обрыв сети. */
  offline = false;
  /** Не отвечать вообще — «чёрная дыра»: сокет жив, ответа нет. */
  blackhole = false;
  /** Счётчики POST-ов по путям. */
  posts: string[] = [];

  readonly fetchImpl: AdminFetch = async (url, init) => {
    this.calls.push({ url, init });
    if (init.method === 'POST') this.posts.push(new URL(url, 'http://x').pathname);
    if (this.blackhole) return new Promise(() => {});
    if (this.offline) throw new TypeError('Failed to fetch');
    const status = this.status;
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => (init.method === 'POST' ? { ok: true } : this.body),
    };
  };

  get queueCalls(): Call[] {
    return this.calls.filter((c) => c.init.method === 'GET');
  }
}

function harness(token = TOKEN) {
  const clock = new ManualClock(1_700_000_000_000);
  const gateway = new Gateway();
  const storage = memoryTokenStorage(token);
  const session = new AdminSession({
    clock,
    fetchImpl: gateway.fetchImpl,
    storage,
    pollIntervalMs: POLL_MS,
    requestTimeoutMs: REQUEST_TIMEOUT_MS,
  });
  const seen: AdminState[] = [];
  session.subscribe((s) => seen.push(s));
  return { clock, gateway, storage, session, seen };
}

/**
 * Прокручивает микрозадачи. Опрос идёт через несколько await-ов подряд
 * (fetch → json → patch), поэтому одного тика не хватает, а реального
 * времени здесь нет вовсе — часы ручные.
 */
async function settle(turns = 8): Promise<void> {
  for (let i = 0; i < turns; i++) await Promise.resolve();
}

/** Двигает часы на один период опроса и даёт ответу дойти. */
async function tickPoll(clock: ManualClock, times = 1): Promise<void> {
  for (let i = 0; i < times; i++) {
    clock.advance(POLL_MS);
    await settle();
  }
}

/* ------------------------------------------------------------------ разбор */

describe('разбор GET /api/admin/queue', () => {
  it('читает форму шлюза целиком', () => {
    const s = parseAdminSnapshot(snapshotBody());
    expect(s.serverNow).toBe(1_700_000_000);
    expect(s.robot).toBe('idle');
    expect(s.estop).toBe(false);
    expect(s.controller?.token).toBe('3f2504e0-4f89-11d3-9a0c-0305e82c3301');
    expect(s.controller?.remainingSec).toBe(60);
    expect(s.controller?.idleForSec).toBe(3);
    expect(s.queue).toHaveLength(2);
    expect(s.queue[1].connected).toBe(false);
    expect(s.observers).toEqual(['cccccccc-0000']);
    expect(s.cooldowns).toEqual([{ token: 'dddddddd-0000', secondsLeft: 12.5 }]);
  });

  it('пустой стенд: ни оператора, ни очереди', () => {
    const s = parseAdminSnapshot(snapshotBody({ controller: null, queue: [] }));
    expect(s.controller).toBeNull();
    expect(s.queue).toEqual([]);
    expect(participants(s)).toBe(0);
  });

  it('participants считает оператора вместе с ждущими — как max_queue', () => {
    expect(participants(parseAdminSnapshot(snapshotBody()))).toBe(3);
  });

  it('estop без поля robot всё равно виден как estop', () => {
    const s = parseAdminSnapshot({ estop: true, queue: [] });
    expect(s.estop).toBe(true);
    expect(s.robot).toBe('estop');
  });

  it('мусор вместо тела не роняет панель', () => {
    expect(parseAdminSnapshot(null).queue).toEqual([]);
    expect(parseAdminSnapshot('нет').controller).toBeNull();
    expect(parseAdminSnapshot({ queue: 'что-то' }).queue).toEqual([]);
  });

  it('терпит старую форму, где controller — просто строка', () => {
    const s = parseAdminSnapshot({
      controller: 'tok-1',
      expires_at: 1_700_000_030,
      queue: ['tok-2'],
      estop: false,
    });
    expect(s.controller?.token).toBe('tok-1');
    expect(s.controller?.expiresAt).toBe(1_700_000_030);
    expect(s.queue[0]).toEqual({
      token: 'tok-2',
      position: 1,
      etaSec: null,
      connected: true,
    });
  });

  it('токен в таблице показывается коротким', () => {
    expect(shortToken('3f2504e0-4f89-11d3')).toBe('3f2504e0…');
    expect(shortToken('bot-1')).toBe('bot-1');
  });
});

/* ------------------------------------------------------------------ токен */

describe('токен', () => {
  it('уходит заголовком X-Admin-Token и никогда не в URL', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();

    const call = gateway.queueCalls[0];
    expect(call.init.headers['X-Admin-Token']).toBe(TOKEN);
    expect(call.url).toBe('/api/admin/queue');
    expect(call.url).not.toContain(TOKEN);
  });

  it('без токена не делает ни одного запроса', async () => {
    const { session, gateway } = harness('');
    session.start();
    await settle();

    expect(gateway.calls).toHaveLength(0);
    expect(session.getState().link).toBe('idle');
  });

  it('сохраняется в хранилище только после удачного ответа', async () => {
    const { session, storage } = harness('');
    storage.clear();
    session.setToken('  свежий-токен  ');
    // Пробелы по краям — обычная беда при копировании из чата.
    expect(session.getState().token).toBe('свежий-токен');
    expect(storage.read()).toBe('');

    session.retry();
    await settle();
    expect(storage.read()).toBe('свежий-токен');
  });

  it('неверный токен: 403 гасит опрос, стирает сохранённое и просит ввести заново', async () => {
    const { session, gateway, storage, clock } = harness();
    gateway.status = 403;
    session.start();
    await settle();

    const s = session.getState();
    expect(s.link).toBe('forbidden');
    expect(s.authorized).toBe(false);
    expect(s.snapshot).toBeNull();
    expect(storage.read()).toBe('');
    // Долбить шлюз 403-ми бессмысленно: следующего опроса быть не должно.
    const before = gateway.queueCalls.length;
    clock.advance(POLL_MS * 5);
    await settle();
    expect(gateway.queueCalls.length).toBe(before);
  });

  it('после исправления токена опрос возобновляется', async () => {
    const { session, gateway } = harness();
    gateway.status = 403;
    session.start();
    await settle();
    expect(session.getState().link).toBe('forbidden');

    gateway.status = 200;
    session.setToken('правильный');
    session.retry();
    await settle();

    expect(session.getState().link).toBe('ok');
    expect(session.getState().authorized).toBe(true);
  });

  it('выход забывает токен и снимок', async () => {
    const { session, storage } = harness();
    session.start();
    await settle();
    expect(storage.read()).toBe(TOKEN);

    session.signOut();
    expect(storage.read()).toBe('');
    expect(session.getState().token).toBe('');
    expect(session.getState().snapshot).toBeNull();
  });
});

/* ------------------------------------------------------------------ опрос */

describe('живое обновление', () => {
  it('опрашивает очередь по таймеру, а не в цикле', async () => {
    const { session, gateway, clock } = harness();
    session.start();
    await settle();
    expect(gateway.queueCalls).toHaveLength(1);

    await tickPoll(clock);
    expect(gateway.queueCalls).toHaveLength(2);

    await tickPoll(clock, 3);
    expect(gateway.queueCalls).toHaveLength(5);
  });

  it('stop() гасит все таймеры — панель не течёт при уходе со страницы', async () => {
    const { session, clock } = harness();
    session.start();
    await settle();
    session.request('estop'); // взводим ещё и таймер подтверждения
    expect(clock.pendingTimers).toBeGreaterThan(0);

    session.stop();
    expect(clock.pendingTimers).toBe(0);
  });

  it('потеря связи не стирает последний снимок, но помечает его устаревшим', async () => {
    const { session, gateway, clock } = harness();
    session.start();
    await settle();
    const fresh = session.getState();
    expect(isSnapshotStale(fresh, clock.now())).toBe(false);

    gateway.offline = true;
    await tickPoll(clock);

    const s = session.getState();
    expect(s.link).toBe('offline');
    // Цифры остались на экране, но панель обязана признать их возраст.
    expect(s.snapshot).not.toBeNull();
    expect(s.failures).toBe(1);
    clock.advance(5000);
    expect(isSnapshotStale(session.getState(), clock.now())).toBe(true);
  });

  it('после обрыва опрос продолжается и связь восстанавливается сама', async () => {
    const { session, gateway, clock } = harness();
    session.start();
    await settle();

    gateway.offline = true;
    await tickPoll(clock);
    expect(session.getState().link).toBe('offline');

    gateway.offline = false;
    await tickPoll(clock);
    expect(session.getState().link).toBe('ok');
    expect(session.getState().failures).toBe(0);
  });

  it('первый опрос показывает «загружаем», а не пустую панель', async () => {
    const { session, seen } = harness();
    session.start();
    expect(session.getState().link).toBe('loading');
    await settle();
    expect(seen.some((s) => s.link === 'loading')).toBe(true);
    expect(session.getState().link).toBe('ok');
  });
});

/* ------------------------------------------------------------------ кнопки */

describe('СТОП требует подтверждения', () => {
  it('первое нажатие НИЧЕГО не отправляет', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();

    session.request('estop');
    await settle();

    expect(gateway.posts).toEqual([]);
    expect(session.getState().pendingConfirm).toBe('estop');
  });

  it('подтверждение отправляет POST /api/admin/estop', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();

    session.request('estop');
    session.confirm();
    await settle();
    await settle();

    expect(gateway.posts).toEqual(['/api/admin/estop']);
    expect(session.getState().pendingConfirm).toBeNull();
    expect(session.getState().notice?.kind).toBe('ok');
  });

  it('отмена не отправляет ничего', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();

    session.request('estop');
    session.cancelConfirm();
    await settle();

    expect(gateway.posts).toEqual([]);
    expect(session.getState().pendingConfirm).toBeNull();
  });

  it('confirm() без взведённого действия — пустышка', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();

    session.confirm();
    await settle();
    expect(gateway.posts).toEqual([]);
  });

  it('взведённая кнопка сама сбрасывается, если к ней не вернулись', async () => {
    const { session, clock } = harness();
    session.start();
    await settle();

    session.request('estop');
    expect(session.getState().pendingConfirm).toBe('estop');
    clock.advance(DEFAULT_CONFIRM_TIMEOUT_MS + 1);
    expect(session.getState().pendingConfirm).toBeNull();
  });

  it('снятие аварийной остановки тоже под подтверждением: она возвращает подвижность', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();

    expect(needsConfirm('release_estop')).toBe(true);
    session.request('release_estop');
    await settle();
    expect(gateway.posts).toEqual([]);

    session.confirm();
    await settle();
    await settle();
    expect(gateway.posts).toEqual(['/api/admin/release_estop']);
  });

  it('снять оператора — штатный жест, подтверждения не требует', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();

    expect(needsConfirm('kick')).toBe(false);
    session.request('kick');
    await settle();
    await settle();

    expect(gateway.posts).toEqual(['/api/admin/kick']);
    expect(session.getState().pendingConfirm).toBeNull();
  });

  it('после действия снимок обновляется сразу, не дожидаясь тика опроса', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();
    const before = gateway.queueCalls.length;

    session.request('kick');
    await settle();
    await settle();

    expect(gateway.queueCalls.length).toBeGreaterThan(before);
  });

  it('403 на действии переводит панель в «нужен токен», а не молчит', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();

    gateway.status = 403;
    session.request('kick');
    await settle();
    await settle();

    expect(session.getState().link).toBe('forbidden');
    expect(session.getState().notice?.kind).toBe('error');
  });

  it('обрыв на действии сообщает о неудаче и не врёт про успех', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();

    gateway.offline = true;
    session.request('kick');
    await settle();
    await settle();

    const s = session.getState();
    expect(s.notice?.kind).toBe('error');
    expect(s.notice?.text).toContain('Снять оператора');
    expect(s.busy).toBeNull();
  });

  it('зависший шлюз не оставляет кнопку заблокированной навсегда', async () => {
    const { session, gateway, clock } = harness();
    session.start();
    await settle();

    // Сеть «чёрная дыра»: соединение есть, ответа нет и ошибки тоже нет.
    gateway.blackhole = true;
    session.request('kick');
    await settle();
    expect(session.getState().busy).toBe('kick');

    clock.advance(REQUEST_TIMEOUT_MS + 1);
    await settle();

    const s = session.getState();
    expect(s.busy).toBeNull();
    expect(s.notice?.kind).toBe('error');
    expect(s.link).toBe('offline');
  });

  it('зависший опрос не останавливает панель насовсем', async () => {
    const { session, gateway, clock } = harness();
    session.start();
    await settle();
    const before = gateway.queueCalls.length;

    gateway.blackhole = true;
    await tickPoll(clock);
    clock.advance(REQUEST_TIMEOUT_MS + 1);
    await settle();
    expect(session.getState().link).toBe('offline');

    // Шлюз ожил — следующий тик опроса всё равно случается.
    gateway.blackhole = false;
    await tickPoll(clock);
    expect(gateway.queueCalls.length).toBeGreaterThan(before + 1);
    expect(session.getState().link).toBe('ok');
  });

  it('пока действие в полёте, второе нажатие игнорируется', async () => {
    const { session, gateway } = harness();
    session.start();
    await settle();

    const first = session.perform('kick');
    session.request('kick');
    session.request('estop');
    await first;
    await settle();

    expect(gateway.posts).toEqual(['/api/admin/kick']);
  });
});

/* ------------------------------------------------------------------ таймеры */

describe('таймеры оператора', () => {
  it('остаток хода считается по часам СЕРВЕРА, а не ноутбука', async () => {
    const { session, clock } = harness();
    session.start();
    await settle();

    // expires_at = now + 60 в шкале сервера; локальные часы совпадают.
    expect(controllerRemainingSec(session.getState(), clock.now())).toBe(60);

    clock.advance(10_000);
    expect(controllerRemainingSec(session.getState(), clock.now())).toBe(50);
  });

  it('расхождение часов ноутбука со шлюзом не ломает отсчёт', async () => {
    const { session, gateway, clock } = harness();
    // Шлюз считает, что сейчас на две минуты позже.
    gateway.body = snapshotBody({
      now: 1_700_000_120,
      controller: {
        token: 'tok',
        connected: true,
        expires_at: 1_700_000_180,
        remaining_sec: 60,
        idle_for_sec: 0,
        disconnected_for_sec: null,
      },
    });
    session.start();
    await settle();

    expect(session.getState().clockOffsetSec).toBeCloseTo(120, 3);
    expect(controllerRemainingSec(session.getState(), clock.now())).toBe(60);
  });

  it('шлюз без now: считаем от remaining_sec и момента получения', async () => {
    const { session, gateway, clock } = harness();
    gateway.body = snapshotBody({
      now: null,
      controller: {
        token: 'tok',
        connected: true,
        expires_at: null,
        remaining_sec: 45,
        idle_for_sec: 0,
        disconnected_for_sec: null,
      },
    });
    session.start();
    await settle();

    expect(controllerRemainingSec(session.getState(), clock.now())).toBe(45);
    clock.advance(5000);
    expect(controllerRemainingSec(session.getState(), clock.now())).toBe(40);
  });

  it('без оператора остаток — ноль, а не отрицательное число', async () => {
    const { session, gateway, clock } = harness();
    gateway.body = snapshotBody({ controller: null });
    session.start();
    await settle();
    expect(controllerRemainingSec(session.getState(), clock.now())).toBe(0);
  });

  it('бездействие оператора продолжает тикать между опросами', async () => {
    const { session, clock } = harness();
    session.start();
    await settle();
    expect(controllerIdleSec(session.getState(), clock.now())).toBeCloseTo(3, 3);
    clock.advance(2000);
    expect(controllerIdleSec(session.getState(), clock.now())).toBeCloseTo(5, 3);
  });
});
