import { useEffect } from 'react';
import type { Toast as ToastData } from '../lib/machine';

interface Props {
  toast: ToastData | null;
  onDismiss: () => void;
  /**
   * Не показывать, но продолжать отсчитывать время жизни. Нужно, когда экран
   * уже объяснил то же самое плашкой: повторять это поверх джойстика — шум,
   * а бросить тост в состоянии без таймера значило бы оставить подсветку
   * органа висеть до конца хода.
   */
  muted?: boolean;
}

/**
 * Всплывающая подсказка по кодам ошибок из контракта.
 * `rate_limited` и `bad_message` сюда не доходят вовсе — они молчаливые.
 */
export function Toast({ toast, onDismiss, muted = false }: Props) {
  useEffect(() => {
    if (!toast || toast.severity === 'banner' || toast.ttlMs <= 0) return;
    const id = setTimeout(onDismiss, toast.ttlMs);
    return () => clearTimeout(id);
  }, [toast, onDismiss]);

  if (!toast || muted) return null;

  return (
    <div
      role="status"
      className="safe-x pointer-events-none fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-[calc(env(safe-area-inset-bottom,0px)+1rem)]"
    >
      <button
        type="button"
        onClick={onDismiss}
        className="animate-rise-in pointer-events-auto w-full max-w-md rounded-2xl bg-ink-900 px-4 py-3 text-left text-white shadow-2xl dark:bg-ink-100 dark:text-ink-950"
      >
        <div className="text-[15px] font-semibold">{toast.title}</div>
        {toast.hint && <div className="mt-0.5 text-sm opacity-80">{toast.hint}</div>}
      </button>
    </div>
  );
}
