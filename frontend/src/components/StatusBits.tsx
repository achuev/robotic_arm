import type { RobotStatus } from '../lib/protocol';

const ROBOT_LABEL: Record<RobotStatus, string> = {
  idle: 'Робот готов',
  moving: 'Рука движется',
  error: 'Ошибка робота',
  estop: 'Аварийная остановка',
};

const ROBOT_DOT: Record<RobotStatus, string> = {
  idle: 'bg-accent-500',
  moving: 'bg-amber-400',
  error: 'bg-red-500',
  estop: 'bg-red-500',
};

export function RobotChip({
  robot,
  online,
  stalled,
}: {
  robot: RobotStatus;
  online: boolean;
  stalled?: boolean;
}) {
  if (stalled) {
    // Сервер заморозил движение, потому что перестал нас слышать (обычно —
    // вкладка ушла в фон и браузер придушил таймеры). Врать про «готов» нельзя.
    return (
      <span className="inline-flex items-center gap-2 rounded-full bg-amber-100 px-3 py-1.5 text-sm font-medium text-amber-900 dark:bg-amber-500/20 dark:text-amber-200">
        <span className="h-2.5 w-2.5 rounded-full bg-amber-500" />
        Движение приостановлено
      </span>
    );
  }
  if (!online) {
    return (
      <span className="inline-flex items-center gap-2 rounded-full bg-ink-200 px-3 py-1.5 text-sm font-medium dark:bg-ink-800">
        <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-ink-400" />
        Восстанавливаем связь
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-2 rounded-full bg-ink-100 px-3 py-1.5 text-sm font-medium dark:bg-ink-800">
      <span className={`h-2.5 w-2.5 rounded-full ${ROBOT_DOT[robot]}`} />
      {ROBOT_LABEL[robot]}
    </span>
  );
}

export function EstopBanner() {
  return (
    <div
      role="alert"
      className="safe-x bg-red-600 px-4 py-3 text-center text-white"
    >
      <div className="text-base font-bold">Робот остановлен оператором</div>
      <div className="text-sm opacity-90">
        Это аварийная остановка. Управление вернётся, когда её снимут.
      </div>
    </div>
  );
}

/** Экран 1: короткий скелетон вместо спиннера на весь экран. */
export function ConnectingSkeleton() {
  return (
    <div className="flex flex-col gap-3 p-4" aria-busy="true" aria-label="Подключаемся">
      <div className="skeleton aspect-[4/3] w-full rounded-2xl" />
      <div className="skeleton h-6 w-40" />
      <div className="skeleton h-16 w-full" />
      <div className="skeleton h-16 w-full" />
    </div>
  );
}

