import { useEffect, useMemo, useState } from 'react';
import QRCode from 'qrcode';
import { fetchStatus, publicControlUrl } from '../lib/api';
import { formatWait, peopleAhead } from '../lib/format';
import type { StatusResponse } from '../lib/protocol';

const POLL_MS = 3000;

/**
 * Экран для большого монитора рядом со стендом.
 * Задача ровно одна: человек проходит мимо, видит QR и понимает, что делать.
 */
export function Landing() {
  const url = useMemo(() => publicControlUrl(), []);
  const [qr, setQr] = useState<string | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    QRCode.toString(url, {
      type: 'svg',
      errorCorrectionLevel: 'M',
      margin: 1,
      color: { dark: '#0e1118', light: '#ffffff' },
    })
      .then(setQr)
      .catch(() => setQr(null));
  }, [url]);

  useEffect(() => {
    let stopped = false;
    const abort = new AbortController();

    const poll = async () => {
      try {
        const next = await fetchStatus(abort.signal);
        if (!stopped) {
          setStatus(next);
          setOffline(false);
        }
      } catch {
        if (!stopped) setOffline(true);
      }
    };

    poll();
    const id = setInterval(poll, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(id);
      abort.abort();
    };
  }, []);

  const occupied = status?.occupied === true;
  const queueLength = status?.queue_length ?? 0;
  const estop = status?.robot === 'estop' || status?.robot === 'error';

  return (
    <div className="safe-x safe-top safe-bottom flex min-h-dvh flex-col items-center justify-center gap-10 p-8 lg:flex-row lg:gap-20">
      <div className="text-center lg:text-left">
        <p className="text-lg font-medium uppercase tracking-[0.2em] text-accent-600 dark:text-accent-400">
          Живой робот
        </p>
        <h1 className="mt-3 text-5xl font-black leading-[1.05] lg:text-7xl">
          Наведите камеру
          <br />и управляйте
          <br />
          рукой
        </h1>
        <p className="mt-5 max-w-md text-xl text-ink-500 dark:text-ink-400">
          Никаких приложений и регистрации. У вас будет 90 секунд.
        </p>

        <div className="mt-8 flex flex-col items-center gap-3 lg:items-start">
          <StatusPill
            offline={offline}
            estop={estop}
            occupied={occupied}
            queueLength={queueLength}
          />
          {queueLength > 0 && !offline && (
            <p className="text-lg text-ink-500 dark:text-ink-400">
              В очереди {peopleAhead(queueLength)} · ждать{' '}
              {formatWait(status?.estimated_wait_sec ?? 0)}
            </p>
          )}
        </div>
      </div>

      <div className="flex flex-col items-center gap-4">
        <div className="rounded-3xl bg-white p-5 shadow-2xl">
          {qr ? (
            <div
              className="h-64 w-64 lg:h-80 lg:w-80 [&>svg]:h-full [&>svg]:w-full"
              // qrcode отдаёт статический SVG, собранный нами же из локальной строки.
              dangerouslySetInnerHTML={{ __html: qr }}
            />
          ) : (
            <div className="skeleton h-64 w-64 lg:h-80 lg:w-80" />
          )}
        </div>
        <code className="max-w-[20rem] truncate text-sm text-ink-400">{url}</code>
      </div>
    </div>
  );
}

function StatusPill({
  offline,
  estop,
  occupied,
  queueLength,
}: {
  offline: boolean;
  estop: boolean;
  occupied: boolean;
  queueLength: number;
}) {
  const [dot, text] = offline
    ? ['bg-ink-400', 'Стенд недоступен']
    : estop
      ? ['bg-red-500', 'Робот остановлен']
      : occupied || queueLength > 0
        ? ['bg-amber-400', 'Робот занят']
        : ['bg-accent-500', 'Робот свободен'];

  return (
    <span className="inline-flex items-center gap-3 rounded-full bg-ink-100 px-5 py-3 text-xl font-semibold dark:bg-ink-900">
      <span className={`h-4 w-4 rounded-full ${dot} ${offline ? 'animate-pulse' : ''}`} />
      {text}
    </span>
  );
}
