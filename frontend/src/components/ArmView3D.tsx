import { useEffect, useRef, useState } from 'react';

import type { ArmScene } from './ArmScene';

interface Props {
  /** Текущие углы суставов из `state.joints`. Захват — нормированный 0..1. */
  joints: Record<string, number>;
}

type Phase = 'loading' | 'ready' | 'failed';

/**
 * 3D-модель руки. Живёт рядом с видео и показывает ту же руку, но с любого
 * ракурса и без задержки потока.
 *
 * three.js и модель (около 420 КБ по сети вместе) грузятся ЛЕНИВО, только
 * когда человек открыл эту вкладку. На лендинг и на первый экран управления
 * они не попадают.
 *
 * Углы передаются в сцену напрямую, минуя состояние React: они меняются
 * десять раз в секунду, и перерисовывать на каждый кадр дерево компонентов
 * незачем — меняются только матрицы объектов.
 */
export function ArmView3D({ joints }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const sceneRef = useRef<ArmScene | null>(null);
  const [phase, setPhase] = useState<Phase>('loading');

  useEffect(() => {
    let cancelled = false;
    let scene: ArmScene | null = null;
    let observer: ResizeObserver | null = null;
    let themeQuery: MediaQueryList | null = null;
    let onThemeChange: (() => void) | null = null;

    (async () => {
      const canvas = canvasRef.current;
      if (!canvas) return;

      // Чанк подтягивается здесь — до этого момента three.js в бандле нет.
      const mod = await import('./ArmScene');
      if (cancelled) return;

      if (!mod.webglAvailable()) {
        setPhase('failed');
        return;
      }

      const pickTheme = () =>
        document.documentElement.classList.contains('dark') ||
        window.matchMedia('(prefers-color-scheme: dark)').matches
          ? mod.THEMES.dark
          : mod.THEMES.light;

      try {
        scene = new mod.ArmScene(canvas, pickTheme());
        sceneRef.current = scene;

        // BASE_URL, а не '/': сайт может жить не в корне.
        await scene.load(import.meta.env.BASE_URL ?? '/');
        if (cancelled) {
          scene.dispose();
          return;
        }

        const parent = canvas.parentElement!;
        const applySize = () => {
          const r = parent.getBoundingClientRect();
          scene?.resize(r.width, r.height);
        };
        applySize();
        observer = new ResizeObserver(applySize);
        observer.observe(parent);

        themeQuery = window.matchMedia('(prefers-color-scheme: dark)');
        onThemeChange = () => scene?.applyTheme(pickTheme());
        themeQuery.addEventListener('change', onThemeChange);

        scene.start();
        setPhase('ready');
      } catch (err) {
        console.warn('[arm3d] сцена не поднялась', err);
        scene?.dispose();
        sceneRef.current = null;
        if (!cancelled) setPhase('failed');
      }
    })();

    return () => {
      cancelled = true;
      observer?.disconnect();
      if (themeQuery && onThemeChange) themeQuery.removeEventListener('change', onThemeChange);
      sceneRef.current?.dispose();
      sceneRef.current = null;
    };
  }, []);

  useEffect(() => {
    sceneRef.current?.setJoints(joints);
  }, [joints]);

  return (
    <div className="relative aspect-[4/3] w-full overflow-hidden bg-ink-50 dark:bg-ink-900 lg:aspect-auto lg:h-full lg:min-h-0">
      <canvas
        ref={canvasRef}
        className="h-full w-full touch-none"
        aria-label="Трёхмерная модель руки"
      />

      {phase !== 'ready' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-ink-50 text-ink-400 dark:bg-ink-900">
          {phase === 'loading' ? (
            <>
              <div
                className="h-8 w-8 animate-spin rounded-full border-2 border-ink-300 border-t-transparent"
                aria-hidden
              />
              <p className="text-sm">Загружаем модель…</p>
            </>
          ) : (
            <p className="px-6 text-center text-sm">
              Браузер не поддерживает 3D
              <br />
              <span className="opacity-70">Переключитесь на камеру</span>
            </p>
          )}
        </div>
      )}

      {phase === 'ready' && (
        <p className="pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full bg-black/45 px-3 py-1 text-xs text-white/85">
          Поверните пальцем
        </p>
      )}
    </div>
  );
}
