import { useNow } from '../hooks/useNow';
import { describeRevoke } from '../lib/errors';
import { formatWait, peopleAhead } from '../lib/format';
import { canEnqueue, cooldownRemainingSec, type ClientState } from '../lib/machine';

/* ------------------------------------------------- экран 2: наблюдатель */

export function ObserverCard({
  state,
  onEnqueue,
}: {
  state: ClientState;
  onEnqueue: () => void;
}) {
  const now = useNow(500, state.cooldownUntilMs !== null);
  const allowed = canEnqueue(state, now);
  const busy = state.queue !== null && state.queue.queueLength > 0;
  const occupied = busy || state.robot === 'moving';

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="text-center">
        <h1 className="text-2xl font-bold">
          {occupied ? 'Робот сейчас занят' : 'Робот свободен'}
        </h1>
        <p className="mt-1 text-ink-500 dark:text-ink-400">
          {occupied
            ? state.queue && state.queue.queueLength > 0
              ? `В очереди ${peopleAhead(state.queue.queueLength)}`
              : 'Подождите пару секунд'
            : 'Можно взять управление прямо сейчас'}
        </p>
      </div>

      <button
        type="button"
        disabled={!allowed}
        onClick={onEnqueue}
        className="h-20 w-full rounded-2xl bg-accent-500 text-xl font-bold text-white shadow-lg transition active:scale-[0.98] disabled:bg-ink-300 disabled:text-ink-500 disabled:shadow-none dark:disabled:bg-ink-800"
      >
        {state.estop
          ? 'Робот остановлен'
          : !state.online
            ? 'Восстанавливаем связь…'
            : occupied
              ? 'Встать в очередь'
              : 'Взять управление'}
      </button>
    </div>
  );
}

/* ------------------------------------------------- экран 3: в очереди */

export function QueueCard({
  state,
  onLeave,
}: {
  state: ClientState;
  onLeave: () => void;
}) {
  const position = state.queue?.position ?? 1;
  const ahead = state.queue?.ahead ?? Math.max(0, position - 1);
  const eta = state.queue?.etaSec ?? 0;

  return (
    <div className="flex flex-col items-center gap-5 p-6 text-center">
      {/* Спокойно и понятно: это не ошибка, это просто очередь. */}
      <div className="flex h-28 w-28 items-center justify-center rounded-full bg-accent-500/15 text-accent-600 dark:text-accent-400">
        <div>
          <div className="text-4xl font-bold tabular-nums">{position}</div>
          <div className="text-xs font-medium uppercase tracking-wide">в очереди</div>
        </div>
      </div>

      <div>
        <h1 className="text-2xl font-bold">Роботом управляет кто-то другой</h1>
        <p className="mt-2 text-ink-500 dark:text-ink-400">
          {ahead === 0
            ? 'Вы следующий — ход перейдёт к вам автоматически.'
            : `Впереди ${peopleAhead(ahead)}. Ход перейдёт к вам сам, ничего нажимать не нужно.`}
        </p>
      </div>

      <div className="w-full rounded-2xl bg-ink-100 px-4 py-3 dark:bg-ink-800">
        <div className="text-sm text-ink-500 dark:text-ink-400">Ваша очередь</div>
        <div className="text-xl font-semibold">{formatWait(eta)}</div>
      </div>

      <p className="text-sm text-ink-400">Можно смотреть — робот в кадре выше.</p>

      <button
        type="button"
        onClick={onLeave}
        className="text-sm font-medium text-ink-500 underline underline-offset-4 dark:text-ink-400"
      >
        Выйти из очереди
      </button>
    </div>
  );
}

/* ------------------------------------------------- экран 6: вытеснены другой вкладкой */

/**
 * Ту же ссылку открыли во второй вкладке. Ход и место в очереди никуда не
 * делись — они за токеном, а не за вкладкой. Поэтому здесь нет ни ошибки,
 * ни отсчёта: просто спокойное объяснение и кнопка вернуть управление сюда.
 */
export function DisplacedCard({ onResume }: { onResume: () => void }) {
  return (
    <div className="flex flex-col items-center gap-5 p-6 text-center">
      <div className="flex h-24 w-24 items-center justify-center rounded-full bg-ink-100 dark:bg-ink-800">
        <svg viewBox="0 0 48 48" className="h-12 w-12 text-ink-400" aria-hidden>
          <rect x="6" y="10" width="24" height="20" rx="4"
            className="fill-none stroke-current" strokeWidth="3" />
          <rect x="18" y="18" width="24" height="20" rx="4"
            className="fill-none stroke-current" strokeWidth="3" />
        </svg>
      </div>

      <div>
        <h1 className="text-2xl font-bold">Управление продолжено в другой вкладке</h1>
        <p className="mt-2 text-ink-500 dark:text-ink-400">
          Вы открыли этот адрес ещё раз. Ход и место в очереди сохранены — они
          закреплены за вами, а не за вкладкой.
        </p>
      </div>

      <button
        type="button"
        onClick={onResume}
        className="h-20 w-full rounded-2xl bg-accent-500 text-xl font-bold text-white shadow-lg transition active:scale-[0.98]"
      >
        Продолжить здесь
      </button>
    </div>
  );
}

/* ------------------------------------------------- экран 5: время вышло */

export function ExpiredCard({
  state,
  onEnqueue,
  onWatch,
}: {
  state: ClientState;
  onEnqueue: () => void;
  onWatch: () => void;
}) {
  const now = useNow(250, true);
  const copy = describeRevoke(state.lastRevoke ?? 'timeout');
  const left = cooldownRemainingSec(state, now);
  const allowed = canEnqueue(state, now);

  return (
    <div className="flex flex-col items-center gap-5 p-6 text-center">
      <div className="flex h-24 w-24 items-center justify-center rounded-full bg-ink-100 dark:bg-ink-800">
        <svg viewBox="0 0 48 48" className="h-12 w-12 text-ink-400" aria-hidden>
          <circle cx="24" cy="26" r="17" className="fill-none stroke-current" strokeWidth="3" />
          <path d="M24 17v9l6 4" className="fill-none stroke-current" strokeWidth="3" strokeLinecap="round" />
          <path d="M18 5h12" className="stroke-current" strokeWidth="3" strokeLinecap="round" />
        </svg>
      </div>

      <div>
        <h1 className="text-2xl font-bold">{copy.title}</h1>
        <p className="mt-2 text-ink-500 dark:text-ink-400">{copy.hint}</p>
      </div>

      <button
        type="button"
        disabled={!allowed}
        onClick={onEnqueue}
        className="h-20 w-full rounded-2xl bg-accent-500 text-xl font-bold text-white shadow-lg transition active:scale-[0.98] disabled:bg-ink-300 disabled:text-ink-500 disabled:shadow-none dark:disabled:bg-ink-800"
      >
        {left > 0 ? `Снова в очередь через ${left}` : 'Встать в очередь снова'}
      </button>

      {left > 0 && (
        <p className="-mt-2 text-sm text-ink-400">
          Небольшая пауза, чтобы успели попробовать другие.
        </p>
      )}

      <button
        type="button"
        onClick={onWatch}
        className="text-sm font-medium text-ink-500 underline underline-offset-4 dark:text-ink-400"
      >
        Просто посмотреть
      </button>
    </div>
  );
}
