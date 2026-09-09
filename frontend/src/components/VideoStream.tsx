import { useEffect, useState } from 'react';

interface Props {
  src: string | null;
  /** `config.features.video` — поток вообще предусмотрен. */
  enabled: boolean;
}

/**
 * MJPEG-поток обычным `<img>`. Через WebSocket видео не идёт (контракт §4).
 * Если поток отвалился — показываем спокойную заглушку и раз в 5 секунд
 * пробуем снова, меняя ключ в URL.
 */
export function VideoStream({ src, enabled }: Props) {
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!failed || !enabled) return;
    const id = setTimeout(() => {
      setFailed(false);
      setAttempt((n) => n + 1);
    }, 5000);
    return () => clearTimeout(id);
  }, [failed, enabled]);

  useEffect(() => {
    setFailed(false);
  }, [src]);

  const showPlaceholder = !enabled || !src || failed;

  return (
    <div className="relative aspect-[4/3] w-full overflow-hidden bg-ink-900 dark:bg-black">
      {!showPlaceholder && (
        <img
          key={`${src}#${attempt}`}
          src={attempt === 0 ? src! : `${src}${src!.includes('?') ? '&' : '?'}r=${attempt}`}
          alt="Робот в прямом эфире"
          className="h-full w-full object-contain"
          onError={() => setFailed(true)}
        />
      )}

      {showPlaceholder && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-ink-400">
          <svg viewBox="0 0 64 64" className="h-16 w-16" aria-hidden>
            <rect
              x="6" y="16" width="42" height="32" rx="6"
              className="fill-none stroke-current" strokeWidth="3"
            />
            <path d="M48 30l12-8v20l-12-8z" className="fill-none stroke-current" strokeWidth="3" />
            <path d="M12 52L52 12" className="stroke-current" strokeWidth="3" strokeLinecap="round" />
          </svg>
          <p className="px-6 text-center text-sm">
            {enabled ? 'Камера не отвечает' : 'Видео на этом стенде не включено'}
            <br />
            <span className="opacity-70">Роботом всё равно можно управлять</span>
          </p>
        </div>
      )}
    </div>
  );
}
