import { describe, expect, it } from 'vitest';
import { clamp, parseServerMessage } from './protocol';

describe('разбор кадров WebSocket', () => {
  it('пропускает все типы из контракта', () => {
    const frames = [
      { t: 'state', joints: {}, ee: { x: 0, y: 0, z: 0 }, robot: 'idle', ts: 1 },
      { t: 'queue', position: 1, ahead: 0, eta_sec: 90, queue_length: 1, you_control: false },
      { t: 'granted', duration_sec: 90, expires_at: 100 },
      { t: 'revoked', reason: 'timeout' },
      { t: 'pong', ts: 1 },
      { t: 'error', code: 'estop', message: 'stopped' },
    ];
    for (const f of frames) {
      expect(parseServerMessage(JSON.stringify(f))?.t).toBe(f.t);
    }
  });

  it('неизвестный t возвращает null — соединение при этом рвать нельзя', () => {
    expect(parseServerMessage(JSON.stringify({ t: 'future_thing' }))).toBeNull();
  });

  it('битый JSON, не-объект и отсутствие t дают null, а не исключение', () => {
    expect(parseServerMessage('{oops')).toBeNull();
    expect(parseServerMessage('"строка"')).toBeNull();
    expect(parseServerMessage('null')).toBeNull();
    expect(parseServerMessage(JSON.stringify({ no: 'type' }))).toBeNull();
  });
});

describe('clamp', () => {
  it('зажимает в пределы', () => {
    expect(clamp(5, -1, 1)).toBe(1);
    expect(clamp(-5, -1, 1)).toBe(-1);
    expect(clamp(0.5, -1, 1)).toBe(0.5);
  });
  it('NaN не утекает в протокол', () => {
    expect(clamp(NaN, -1, 1)).toBe(-1);
  });
});
