import { useEffect, useRef, useState } from 'react';
import { ensureToken, fetchConfig, readStoredToken, websocketUrl } from '../lib/api';
import { ConnectionManager } from '../lib/connection';
import { initialState, type ClientState } from '../lib/machine';
import { withConfigDefaults, type AppConfig } from '../lib/protocol';

export type Bootstrap =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; config: AppConfig };

export interface GatewayOptions {
  /**
   * Откуда взять токен для `hello`.
   *
   * По умолчанию — токен посетителя из `POST /api/session`. Админской панели
   * нужен свой: контракт §4 велит вытеснять прежнее соединение с тем же
   * токеном, и панель, открытая рядом с `/control`, выбивала бы вкладку
   * управления.
   */
  getToken?: (signal?: AbortSignal) => Promise<string> | string;
}

/**
 * Поднимает соединение со шлюзом: конфиг + токен → WebSocket.
 * Конфиг читается ровно один раз, как и написано в контракте.
 */
export function useGateway(options: GatewayOptions = {}) {
  const [boot, setBoot] = useState<Bootstrap>({ status: 'loading' });
  const [state, setState] = useState<ClientState>(initialState);
  const cmRef = useRef<ConnectionManager | null>(null);

  // Провайдер токена берём один раз: смена его на лету означала бы смену
  // личности посреди хода, а это ровно то, чего контракт велит избегать.
  const getTokenRef = useRef(options.getToken);

  useEffect(() => {
    let cancelled = false;
    const abort = new AbortController();

    (async () => {
      try {
        const config = withConfigDefaults(await fetchConfig(abort.signal));
        const custom = getTokenRef.current;
        const token = custom
          ? await custom(abort.signal)
          : await ensureToken(abort.signal);
        if (cancelled) return;

        const cm = new ConnectionManager({
          url: websocketUrl(),
          // Читаем токен заново на каждое переподключение: он мог обновиться
          // в другой вкладке, а вернуть ход способен только совпадающий токен.
          getToken: () => (custom ? token : (readStoredToken() ?? token)),
          joints: config.joints,
          // Все тайминги и лимиты — из конфига, ничего не зашито в клиент.
          cooldownSec: config.cooldown_sec,
          watchdogSec: config.watchdog_timeout_sec,
          maxMsgPerSec: config.max_msg_per_sec,
        });
        cmRef.current = cm;
        cm.subscribe(setState);
        cm.start();
        setBoot({ status: 'ready', config });
      } catch (err) {
        if (cancelled) return;
        setBoot({
          status: 'error',
          message:
            err instanceof Error && err.name === 'AbortError'
              ? 'Соединение прервано'
              : 'Не удаётся связаться с роботом',
        });
      }
    })();

    return () => {
      cancelled = true;
      abort.abort();
      cmRef.current?.stop();
      cmRef.current = null;
    };
  }, []);

  return { boot, state, cm: cmRef.current };
}
