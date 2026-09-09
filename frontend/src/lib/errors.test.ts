import { describe, expect, it } from 'vitest';
import { describeError, describeRevoke, parseCooldownSeconds } from './errors';
import {
  cooldownRemainingSec,
  initialState,
  reduce,
  type ClientState,
} from './machine';
import { ERROR_CODES, REVOKE_REASONS, type ErrorCode } from './protocol';

const NOW = 1_700_000_000_000;
const ctx = { now: NOW, cooldownSec: 30 };

function withError(
  code: string,
  message = '',
  start?: ClientState,
  extra: Record<string, unknown> = {}
): ClientState {
  return reduce(
    start ?? { ...initialState, phase: 'observer', online: true, everConnected: true },
    {
      type: 'server',
      msg: { t: 'error', code: code as ErrorCode, message, ...extra },
    },
    ctx
  );
}

const controlling: ClientState = {
  ...initialState,
  phase: 'controlling',
  online: true,
  everConnected: true,
  control: { durationSec: 90, expiresAt: NOW / 1000 + 90 },
};

describe('каждый код ошибки из контракта обработан', () => {
  it('таблица покрывает ровно тот набор кодов, что в docs/api.md', () => {
    for (const code of ERROR_CODES) {
      const p = describeError(code);
      expect(p.code).toBe(code);
      // Либо мы что-то показываем, либо это осознанно молчаливый код.
      if (p.severity === 'silent') {
        expect(['rate_limited', 'bad_message']).toContain(code);
      } else {
        expect(p.title.length).toBeGreaterThan(0);
        expect(p.hint.length).toBeGreaterThan(0);
      }
    }
  });

  it('весь видимый текст — по-русски', () => {
    const cyrillic = /[А-Яа-яЁё]/;
    for (const code of ERROR_CODES) {
      const p = describeError(code);
      if (p.severity === 'silent') continue;
      expect(cyrillic.test(p.title), `title для ${code}`).toBe(true);
      expect(cyrillic.test(p.hint), `hint для ${code}`).toBe(true);
    }
  });

  it('неизвестный код не роняет UI', () => {
    const p = describeError('teapot_on_fire');
    expect(p.severity).toBe('toast');
    expect(p.title.length).toBeGreaterThan(0);
    expect(() => withError('teapot_on_fire')).not.toThrow();
  });
});

describe('реакция состояния на каждый код', () => {
  it('not_controller: снимает нас с управления и объясняет почему', () => {
    const s = withError('not_controller', '', controlling);
    expect(s.phase).toBe('observer');
    expect(s.control).toBeNull();
    expect(s.toast?.title).toBe('Роботом управляет кто-то другой');
  });

  it('not_controller возвращает в очередь, если мы там стоим', () => {
    const queued: ClientState = {
      ...controlling,
      queue: { position: 2, ahead: 1, etaSec: 100, queueLength: 3 },
    };
    expect(withError('not_controller', '', queued).phase).toBe('queued');
  });

  it('not_queued: предлагает встать в очередь', () => {
    const s = withError('not_queued');
    expect(s.phase).toBe('observer');
    expect(s.pendingEnqueue).toBe(false);
    expect(s.toast?.severity).toBe('toast');
  });

  it('queue_full: сообщает и разблокирует кнопку', () => {
    const pending: ClientState = {
      ...initialState,
      phase: 'observer',
      online: true,
      pendingEnqueue: true,
    };
    const s = withError('queue_full', '', pending);
    expect(s.pendingEnqueue).toBe(false);
    expect(s.toast?.title).toContain('Слишком много');
  });

  it('cooldown: берёт секунды из retry_after_sec, а не из текста', () => {
    // Поле контракта важнее: текст может быть любым, в том числе с другим числом.
    const s = withError('cooldown', 'Подождите ещё 99 с', undefined, {
      retry_after_sec: 12,
    });
    expect(s.cooldownUntilMs).toBe(NOW + 12_000);
  });

  it('cooldown: retry_after_sec = 0 означает «уже можно»', () => {
    const s = withError('cooldown', '', undefined, { retry_after_sec: 0 });
    expect(s.cooldownUntilMs).toBe(NOW);
    expect(cooldownRemainingSec(s, NOW)).toBe(0);
  });

  it('cooldown без retry_after_sec подбирает число из текста', () => {
    const s = withError('cooldown', 'Подождите ещё 12 с');
    expect(s.cooldownUntilMs).toBe(NOW + 12_000);
  });

  it('cooldown совсем без подсказок откатывается на 30 с из контракта', () => {
    const s = withError('cooldown', 'нельзя');
    expect(s.cooldownUntilMs).toBe(NOW + 30_000);
  });

  it('rate_limited: молча, без единого пикселя на экране', () => {
    const before = { ...initialState, phase: 'controlling' as const, online: true };
    const s = withError('rate_limited', '', before);
    expect(s.toast).toBeNull();
    expect(s.phase).toBe('controlling');
    expect(describeError('rate_limited').effect).toBe('backoff');
  });

  it('out_of_range: подсвечивает сустав, НАЗВАННЫЙ сервером', () => {
    const s = withError('out_of_range', '', controlling, { joint: 'elbow_flex' });
    expect(s.highlight).toEqual({ kind: 'joint', name: 'elbow_flex' });
    expect(s.phase).toBe('controlling');
  });

  it('out_of_range: для джоггинга подсвечивает ось, а не сустав', () => {
    const s = withError('out_of_range', '', controlling, { axis: 'z' });
    expect(s.highlight).toEqual({ kind: 'axis', name: 'z' });
  });

  it('out_of_range без адреса ничего не подсвечивает вместо того, чтобы гадать', () => {
    const s = withError('out_of_range', '', controlling);
    expect(s.highlight).toBeNull();
  });

  it('granted снимает подсветку с прошлого хода', () => {
    const highlighted = withError('out_of_range', '', controlling, {
      joint: 'elbow_flex',
    });
    const s = reduce(
      highlighted,
      { type: 'server', msg: { t: 'granted', duration_sec: 90, expires_at: 1 } },
      ctx
    );
    expect(s.highlight).toBeNull();
  });

  it('ik_failed: объясняет, что рука не дотянется, и ход не отбирает', () => {
    const s = withError('ik_failed', '', controlling);
    expect(s.phase).toBe('controlling');
    expect(s.toast?.title).toBe('Сюда рука не дотянется');
  });

  it('estop: поднимает баннер и запрещает команды', () => {
    const s = withError('estop', '', controlling);
    expect(s.estop).toBe(true);
    expect(s.toast?.severity).toBe('banner');
  });

  it('bad_message: молчит, а чинится переподключением', () => {
    const s = withError('bad_message');
    expect(s.toast).toBeNull();
    expect(describeError('bad_message').effect).toBe('reconnect');
  });
});

describe('parseCooldownSeconds', () => {
  it('retry_after_sec из контракта важнее текста', () => {
    expect(
      parseCooldownSeconds(
        { t: 'error', code: 'cooldown', message: 'ещё 99 с', retry_after_sec: 7 },
        30
      )
    ).toBe(7);
  });

  it('находит целое число', () => {
    expect(parseCooldownSeconds('осталось 7 секунд', 30)).toBe(7);
  });
  it('округляет дробное вверх', () => {
    expect(parseCooldownSeconds('ещё 4.2 с', 30)).toBe(5);
  });
  it('понимает запятую как разделитель', () => {
    expect(parseCooldownSeconds('ещё 4,2 с', 30)).toBe(5);
  });
  it('без числа отдаёт значение по умолчанию', () => {
    expect(parseCooldownSeconds('подождите', 30)).toBe(30);
  });
});

describe('причины отзыва хода', () => {
  it('на каждую причину есть человеческий русский текст', () => {
    const cyrillic = /[А-Яа-яЁё]/;
    for (const reason of REVOKE_REASONS) {
      const copy = describeRevoke(reason);
      expect(cyrillic.test(copy.title), reason).toBe(true);
      expect(cyrillic.test(copy.hint), reason).toBe(true);
    }
  });

  it('cooldown назначается только за свой израсходованный ход', () => {
    expect(describeRevoke('timeout').cooldown).toBe(true);
    expect(describeRevoke('idle').cooldown).toBe(true);
    expect(describeRevoke('release').cooldown).toBe(true);
    // Это не вина человека — не наказываем.
    expect(describeRevoke('admin').cooldown).toBe(false);
    expect(describeRevoke('estop').cooldown).toBe(false);
    expect(describeRevoke('disconnect').cooldown).toBe(false);
  });

  it('неизвестная причина не роняет экран', () => {
    expect(describeRevoke('solar_flare').title.length).toBeGreaterThan(0);
  });
});
