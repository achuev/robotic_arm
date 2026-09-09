import { useEffect, useState } from 'react';
import { Admin } from './pages/Admin';
import { Control } from './pages/Control';
import { Landing } from './pages/Landing';

/**
 * Роутер на три экрана. Отдельная библиотека здесь была бы дороже пользы:
 * вложенности нет, переходов между экранами в норме тоже нет — лендинг живёт
 * на большом экране, /control открывается по QR на телефоне, а /admin
 * открывает с ноутбука тот, кто стоит у стенда.
 */
export function App() {
  const [path, setPath] = useState(() => window.location.pathname);

  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const normalized = path.replace(/\/+$/, '') || '/';
  if (normalized === '/control') return <Control />;
  if (normalized === '/admin') return <Admin />;
  return <Landing />;
}
