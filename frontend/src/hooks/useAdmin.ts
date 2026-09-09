import { useEffect, useRef, useState } from 'react';
import { AdminSession, type AdminState } from '../lib/admin';

/**
 * Обвязка `AdminSession` для React. Ровно как с `ConnectionManager`:
 * вся логика живёт в библиотеке и покрыта тестами, здесь только подписка.
 */
export function useAdmin(): { state: AdminState; session: AdminSession } {
  const ref = useRef<AdminSession | null>(null);
  if (ref.current === null) ref.current = new AdminSession();
  const session = ref.current;

  const [state, setState] = useState<AdminState>(() => session.getState());

  useEffect(() => {
    const unsubscribe = session.subscribe(setState);
    // Сохранённый токен уже лежит в состоянии — опрос стартует сам.
    session.start();
    return () => {
      unsubscribe();
      session.stop();
    };
  }, [session]);

  return { state, session };
}
