import { useEffect, useMemo, useState } from 'react';
import { useAdmin } from '../hooks/useAdmin';
import { useGateway } from '../hooks/useGateway';
import { useNow } from '../hooks/useNow';
import {
  ACTION_TITLE,
  controllerIdleSec,
  controllerRemainingSec,
  isSnapshotStale,
  participants,
  shortToken,
  snapshotAgeMs,
  type AdminSession,
  type AdminSnapshot,
  type AdminState,
} from '../lib/admin';
import { ensureAdminObserverToken } from '../lib/api';
import { formatWait } from '../lib/format';
import { toDegrees } from '../lib/jointDisplay';
import type { ClientState } from '../lib/machine';
import { GRIPPER_JOINT, type AppConfig, type RobotStatus } from '../lib/protocol';
import { AXES, workspaceEdges } from '../lib/workspace';

/**
 * Пульт для человека, который стоит у стенда.
 *
 * Смотрят его с ноутбука, а не с телефона, поэтому раскладка плотная:
 * всё состояние — на одном экране, без скролла и без раскрывашек. Единственный
 * элемент, который намеренно занимает много места, — «СТОП»: его ищут в панике,
 * и он обязан находиться быстрее, чем человек успеет подумать.
 */
export function Admin() {
  const { state, session } = useAdmin();
  // Свой токен наблюдателя: иначе панель вытеснит вкладку /control на том же
  // ноутбуке (контракт §4 — второе подключение по тому же токену вытесняет).
  const { boot, state: live } = useGateway({ getToken: ensureAdminObserverToken });
  const config = boot.status === 'ready' ? boot.config : null;

  const now = useNow(250, true);

  // Вкладок у оператора обычно несколько — пусть эта называется по делу.
  useEffect(() => {
    document.title = 'Пульт стенда SO-101';
  }, []);

  // Escape — универсальная «отмена»: взведённое подтверждение снимается,
  // не заставляя целиться мышью в маленькую кнопку.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') session.cancelConfirm();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [session]);

  const snapshot = state.snapshot;
  const stale = isSnapshotStale(state, now);
  /**
   * Просить токен заново нужно только тогда, когда его не приняли или его нет.
   * Молчащий шлюз — не повод: токен запомнен, опрос сам восстановится, а
   * человеку у стенда меньше всего нужно набирать пароль в этот момент.
   */
  const showGate =
    !state.authorized && (state.link === 'idle' || state.link === 'forbidden');
  // Пока опрос жив, ему верим больше: у него есть estop прямо из очереди.
  const estop = snapshot?.estop ?? live.estop;

  return (
    <div className="min-h-dvh bg-ink-50 text-ink-900 dark:bg-ink-950 dark:text-ink-50">
      <header className="sticky top-0 z-20 border-b border-ink-200 bg-ink-50/95 backdrop-blur dark:border-ink-800 dark:bg-ink-950/95">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-3 px-4 py-3">
          <div className="mr-auto">
            <h1 className="text-lg font-bold leading-tight">Пульт стенда SO-101</h1>
            <p className="text-xs text-ink-500 dark:text-ink-400">
              Служебная страница. Посетителям её не показывают.
            </p>
          </div>
          <LinkChip state={state} now={now} />
          <WsChip online={live.online} robot={live.robot} />
          {!showGate && (
            <button
              type="button"
              onClick={() => session.signOut()}
              className="rounded-lg border border-ink-300 px-3 py-1.5 text-xs font-semibold text-ink-500 hover:bg-ink-100 dark:border-ink-700 dark:hover:bg-ink-900"
            >
              Забыть токен
            </button>
          )}
        </div>
      </header>

      <main className="mx-auto flex max-w-6xl flex-col gap-4 p-4">
        {/* На экране входа своё сообщение об отказе — дублировать не нужно. */}
        {state.notice && !showGate && (
          <div
            role="status"
            className={`flex items-center justify-between gap-3 rounded-xl px-4 py-2.5 text-sm font-medium ${
              state.notice.kind === 'ok'
                ? 'bg-accent-500/15 text-accent-600 dark:text-accent-400'
                : 'bg-red-500/15 text-red-600 dark:text-red-400'
            }`}
          >
            <span>{state.notice.text}</span>
            <button
              type="button"
              onClick={() => session.dismissNotice()}
              className="shrink-0 text-xs opacity-70 hover:opacity-100"
            >
              скрыть
            </button>
          </div>
        )}

        {showGate ? (
          <TokenGate state={state} session={session} />
        ) : snapshot === null ? (
          // Токен есть, ответа ещё нет. Показывать «управляет: никто» нельзя —
          // мы этого не знаем; честнее сказать, что данных пока нет.
          <WaitingCard state={state} onForget={() => session.signOut()} />
        ) : (
          <>
            {estop && <EstopStrip />}
            {stale && <StaleStrip ageMs={snapshotAgeMs(state, now)} />}

            <div className="grid gap-4 lg:grid-cols-[1fr_20rem]">
              <div className="flex flex-col gap-4">
                <ControllerCard
                  state={state}
                  now={now}
                  onKick={() => session.request('kick')}
                  busy={state.busy === 'kick'}
                />
                <QueueCard snapshot={snapshot} />
                <RobotCard live={live} config={config} snapshot={snapshot} />
              </div>

              <EmergencyPanel state={state} session={session} estop={estop} />
            </div>
          </>
        )}
      </main>
    </div>
  );
}

/* ------------------------------------------------------------------ вход */

function TokenGate({
  state,
  session,
}: {
  state: AdminState;
  session: AdminSession;
}) {
  const [value, setValue] = useState(state.token);

  return (
    <form
      className="card mx-auto mt-10 flex w-full max-w-md flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        session.setToken(value);
        session.retry();
      }}
    >
      <h2 className="text-base font-bold">Админский токен</h2>
      <p className="text-sm text-ink-500 dark:text-ink-400">
        Значение <code>ADMIN_TOKEN</code> из окружения шлюза. Уходит заголовком{' '}
        <code>X-Admin-Token</code>, в адресной строке не появляется.
      </p>
      <input
        type="password"
        value={value}
        autoFocus
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => setValue(e.target.value)}
        placeholder="ADMIN_TOKEN"
        className="h-11 rounded-lg border border-ink-300 bg-white px-3 font-mono text-sm dark:border-ink-700 dark:bg-ink-950"
      />
      {state.link === 'forbidden' && (
        <p className="text-sm font-medium text-red-500">
          Шлюз не принял этот токен. Проверьте <code>ADMIN_TOKEN</code> в{' '}
          <code>.env</code> и перезапустите шлюз, если меняли.
        </p>
      )}
      <button
        type="submit"
        disabled={value.trim().length === 0}
        className="h-11 rounded-lg bg-accent-500 text-sm font-bold text-white disabled:bg-ink-300 dark:disabled:bg-ink-800"
      >
        Войти
      </button>
    </form>
  );
}

/** Токен принят или ещё проверяется, но состояния очереди пока нет. */
function WaitingCard({
  state,
  onForget,
}: {
  state: AdminState;
  onForget: () => void;
}) {
  const offline = state.link === 'offline';
  return (
    <section className="card mx-auto mt-10 flex w-full max-w-md flex-col items-center gap-3 text-center">
      <h2 className="text-base font-bold">
        {offline ? 'Шлюз не отвечает' : 'Читаем состояние очереди…'}
      </h2>
      <p className="text-sm text-ink-500 dark:text-ink-400">
        {offline
          ? `Опрос продолжается, панель подхватит связь сама. Неудачных попыток подряд: ${state.failures}.`
          : 'Секунду.'}
      </p>
      {offline && (
        <button
          type="button"
          onClick={onForget}
          className="h-9 rounded-lg border border-ink-300 px-3 text-xs font-semibold text-ink-500 dark:border-ink-700"
        >
          Ввести другой токен
        </button>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ статус */

function LinkChip({ state, now }: { state: AdminState; now: number }) {
  const age = snapshotAgeMs(state, now);
  const [dot, text] =
    state.link === 'ok'
      ? ['bg-accent-500', `опрос · ${Math.round(age / 100) / 10} с назад`]
      : state.link === 'offline'
        ? ['bg-red-500', 'шлюз не отвечает']
        : state.link === 'forbidden'
          ? ['bg-red-500', 'нужен токен']
          : state.link === 'loading'
            ? ['bg-amber-400', 'опрашиваем…']
            : ['bg-ink-400', 'ожидание'];

  return (
    <span className="inline-flex items-center gap-2 rounded-full bg-ink-100 px-3 py-1.5 text-xs font-medium tabular-nums dark:bg-ink-900">
      <span className={`h-2 w-2 rounded-full ${dot}`} />
      {text}
    </span>
  );
}

const ROBOT_LABEL: Record<RobotStatus, string> = {
  idle: 'готов',
  moving: 'движется',
  error: 'ошибка',
  estop: 'остановлен',
};

function WsChip({ online, robot }: { online: boolean; robot: RobotStatus }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full bg-ink-100 px-3 py-1.5 text-xs font-medium dark:bg-ink-900">
      <span
        className={`h-2 w-2 rounded-full ${online ? 'bg-accent-500' : 'animate-pulse bg-ink-400'}`}
      />
      {online ? `WS · ${ROBOT_LABEL[robot]}` : 'WS · нет связи'}
    </span>
  );
}

function EstopStrip() {
  return (
    <div
      role="alert"
      className="rounded-xl bg-red-600 px-4 py-3 text-sm font-bold text-white"
    >
      Аварийная остановка активна. Команды не публикуются, очередь заморожена —
      люди стоят на своих местах и ждут.
    </div>
  );
}

function StaleStrip({ ageMs }: { ageMs: number }) {
  return (
    <div className="rounded-xl bg-amber-500/15 px-4 py-2.5 text-sm font-medium text-amber-700 dark:text-amber-300">
      Данные не обновлялись {Math.round(ageMs / 1000)} с — шлюз не отвечает.
      Цифрам ниже верить нельзя, кнопки могут не сработать.
    </div>
  );
}

/* ------------------------------------------------------------------ оператор */

function ControllerCard({
  state,
  now,
  onKick,
  busy,
}: {
  state: AdminState;
  now: number;
  onKick: () => void;
  busy: boolean;
}) {
  const c = state.snapshot?.controller ?? null;
  const remaining = controllerRemainingSec(state, now);
  const idle = controllerIdleSec(state, now);

  if (!c) {
    return (
      <section className="card">
        <SectionTitle>Сейчас управляет</SectionTitle>
        <p className="mt-2 text-sm text-ink-500 dark:text-ink-400">
          Никто. Робот свободен — первый, кто нажмёт «Взять управление», получит ход.
        </p>
      </section>
    );
  }

  return (
    <section className="card">
      <SectionTitle>Сейчас управляет</SectionTitle>
      <div className="mt-2 flex flex-wrap items-end gap-x-8 gap-y-3">
        <Metric label="токен" value={shortToken(c.token)} mono />
        <Metric
          label="осталось"
          value={`${remaining} с`}
          tone={remaining <= 15 ? 'warn' : 'normal'}
        />
        <Metric label="бездействует" value={`${idle.toFixed(0)} с`} />
        <Metric
          label="связь"
          value={
            c.connected
              ? 'есть'
              : c.disconnectedForSec === null
                ? 'нет'
                : `нет ${c.disconnectedForSec.toFixed(0)} с`
          }
          tone={c.connected ? 'normal' : 'warn'}
        />
        <button
          type="button"
          onClick={onKick}
          disabled={busy}
          className="ml-auto h-10 rounded-lg border-2 border-ink-300 px-4 text-sm font-bold transition hover:border-ink-400 hover:bg-ink-100 disabled:opacity-50 dark:border-ink-700 dark:hover:bg-ink-800"
        >
          {busy ? 'Снимаем…' : 'Снять оператора'}
        </button>
      </div>
      {!c.connected && (
        <p className="mt-3 text-xs text-ink-500 dark:text-ink-400">
          Оператор в обрыве. Шлюз держит за ним ход ещё несколько секунд
          (RECONNECT_GRACE) — если он не вернётся, ход уйдёт сам.
        </p>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ очередь */

function QueueCard({ snapshot }: { snapshot: AdminSnapshot | null }) {
  const queue = snapshot?.queue ?? [];
  const observers = snapshot?.observers.length ?? 0;
  const cooldowns = snapshot?.cooldowns ?? [];

  return (
    <section className="card">
      <div className="flex items-baseline justify-between">
        <SectionTitle>Очередь</SectionTitle>
        <span className="text-xs text-ink-500 dark:text-ink-400">
          участников {participants(snapshot)} · наблюдателей {observers}
        </span>
      </div>

      {queue.length === 0 ? (
        <p className="mt-2 text-sm text-ink-500 dark:text-ink-400">Пусто.</p>
      ) : (
        <table className="mt-2 w-full text-sm tabular-nums">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-ink-400">
              <th className="w-10 font-medium">№</th>
              <th className="font-medium">токен</th>
              <th className="w-32 font-medium">ждать</th>
              <th className="w-24 font-medium">связь</th>
            </tr>
          </thead>
          <tbody>
            {queue.map((item) => (
              <tr
                key={item.token}
                className="border-t border-ink-100 dark:border-ink-800"
              >
                <td className="py-1.5 font-bold">{item.position}</td>
                <td className="py-1.5 font-mono text-xs">{shortToken(item.token)}</td>
                <td className="py-1.5">
                  {item.etaSec === null ? '—' : formatWait(item.etaSec)}
                </td>
                <td className="py-1.5">
                  {item.connected ? (
                    <span className="text-ink-500 dark:text-ink-400">есть</span>
                  ) : (
                    <span className="font-semibold text-amber-600 dark:text-amber-400">
                      нет
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {cooldowns.length > 0 && (
        <p className="mt-3 text-xs text-ink-500 dark:text-ink-400">
          Отдыхают после своего хода: {cooldowns.length} (дольше всех —{' '}
          {Math.ceil(Math.max(...cooldowns.map((c) => c.secondsLeft)))} с).
        </p>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ робот */

function RobotCard({
  live,
  config,
  snapshot,
}: {
  live: ClientState;
  config: AppConfig | null;
  snapshot: AdminSnapshot | null;
}) {
  const edges = useMemo(
    () => workspaceEdges(live.ee, config?.workspace),
    [live.ee, config]
  );
  const edgeAxes = new Set(edges.map((e) => e.axis));
  const outside = edges.some((e) => e.overshootM > 0);
  const joints = config?.joints.filter((j) => j.name !== GRIPPER_JOINT) ?? [];
  const gripper = live.joints[GRIPPER_JOINT];

  return (
    <section className="card">
      <div className="flex items-baseline justify-between">
        <SectionTitle>Робот</SectionTitle>
        <span className="text-xs text-ink-500 dark:text-ink-400">
          {snapshot?.robot ? ROBOT_LABEL[snapshot.robot] : '—'} · поток{' '}
          {live.online ? 'идёт' : 'молчит'}
        </span>
      </div>

      <div className="mt-2 grid gap-x-6 gap-y-1 text-sm tabular-nums sm:grid-cols-2">
        {joints.map((j) => {
          const v = live.joints[j.name];
          return (
            <div key={j.name} className="flex items-baseline justify-between gap-2">
              <span className="text-ink-500 dark:text-ink-400">{j.label}</span>
              <span className="font-mono text-xs">
                {v === undefined ? '—' : `${toDegrees(v).toFixed(0)}°`}
              </span>
            </div>
          );
        })}
        {gripper !== undefined && (
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-ink-500 dark:text-ink-400">Захват</span>
            <span className="font-mono text-xs">{(gripper * 100).toFixed(0)}%</span>
          </div>
        )}
      </div>

      {live.ee && (
        <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-ink-100 pt-3 text-sm tabular-nums dark:border-ink-800">
          <span className="text-xs uppercase tracking-wide text-ink-400">схват</span>
          {AXES.map((axis) => (
            <span
              key={axis}
              className={`font-mono text-xs ${
                edgeAxes.has(axis)
                  ? 'font-bold text-amber-600 dark:text-amber-400'
                  : ''
              }`}
            >
              {axis} {live.ee![axis].toFixed(3)}
              {config?.workspace && (
                <span className="ml-1 opacity-50">
                  [{config.workspace[axis][0]}…{config.workspace[axis][1]}]
                </span>
              )}
            </span>
          ))}
        </div>
      )}

      {outside && (
        <p className="mt-2 text-xs text-amber-600 dark:text-amber-400">
          Схват вне объявленной рабочей зоны. Так стенд стоит после включения
          (нулевая поза), и так же выходит после команд по суставам — их зона не
          ограничивает, только пределы суставов. Ближайший джог посетителя
          вернёт руку внутрь и по контракту принесёт ему{' '}
          <code>out_of_range</code>; экран управления объясняет это словами, а не
          ошибкой.
        </p>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ СТОП */

function EmergencyPanel({
  state,
  session,
  estop,
}: {
  state: AdminState;
  session: AdminSession;
  estop: boolean;
}) {
  const armed = state.pendingConfirm;
  const busy = state.busy;

  return (
    <aside className="flex flex-col gap-3 lg:sticky lg:top-20 lg:self-start">
      {!estop ? (
        armed === 'estop' ? (
          <div className="rounded-2xl border-4 border-red-600 bg-red-50 p-3 dark:bg-red-950/40">
            <p className="text-center text-sm font-bold text-red-700 dark:text-red-300">
              Остановить робота?
            </p>
            <p className="mt-1 text-center text-xs text-red-700/80 dark:text-red-300/80">
              Текущий оператор потеряет ход, очередь замрёт на местах.
            </p>
            <button
              type="button"
              autoFocus
              onClick={() => session.confirm()}
              disabled={busy !== null}
              className="mt-3 h-24 w-full rounded-xl bg-red-600 text-2xl font-black uppercase tracking-wide text-white shadow-lg transition hover:bg-red-700 active:scale-[0.99] disabled:opacity-60"
            >
              {busy ? 'Останавливаем…' : 'Да, стоп'}
            </button>
            <button
              type="button"
              onClick={() => session.cancelConfirm()}
              className="mt-2 h-9 w-full rounded-lg text-sm font-semibold text-red-700 hover:bg-red-100 dark:text-red-300 dark:hover:bg-red-900/40"
            >
              Отмена (Esc)
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => session.request('estop')}
            disabled={busy !== null}
            className="h-40 w-full rounded-2xl bg-red-600 text-4xl font-black uppercase tracking-[0.1em] text-white shadow-xl transition hover:bg-red-700 active:scale-[0.99] disabled:opacity-60"
          >
            Стоп
          </button>
        )
      ) : armed === 'release_estop' ? (
        <div className="rounded-2xl border-4 border-amber-500 bg-amber-50 p-3 dark:bg-amber-950/40">
          <p className="text-center text-sm font-bold text-amber-800 dark:text-amber-200">
            Снять аварийную остановку?
          </p>
          <p className="mt-1 text-center text-xs text-amber-800/80 dark:text-amber-200/80">
            Рука снова станет подвижной. Уберите руки от робота.
          </p>
          <button
            type="button"
            autoFocus
            onClick={() => session.confirm()}
            disabled={busy !== null}
            className="mt-3 h-16 w-full rounded-xl bg-amber-500 text-lg font-black uppercase text-white transition hover:bg-amber-600 disabled:opacity-60"
          >
            {busy ? 'Снимаем…' : 'Да, снять'}
          </button>
          <button
            type="button"
            onClick={() => session.cancelConfirm()}
            className="mt-2 h-9 w-full rounded-lg text-sm font-semibold text-amber-800 hover:bg-amber-100 dark:text-amber-200 dark:hover:bg-amber-900/40"
          >
            Отмена (Esc)
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => session.request('release_estop')}
          disabled={busy !== null}
          className="h-40 w-full rounded-2xl border-4 border-amber-500 text-2xl font-black uppercase tracking-wide text-amber-600 transition hover:bg-amber-50 disabled:opacity-60 dark:text-amber-400 dark:hover:bg-amber-950/40"
        >
          Снять стоп
        </button>
      )}

      <p className="text-xs leading-relaxed text-ink-500 dark:text-ink-400">
        {estop
          ? 'Робот стоит. Снять остановку можно только отсюда — сам он не оживёт.'
          : 'Останавливает публикацию команд немедленно. Ход отбирается, очередь сохраняется.'}
      </p>

      {armed && (
        <p className="text-xs font-medium text-ink-500 dark:text-ink-400" role="status">
          Ждём подтверждения: {ACTION_TITLE[armed].toLowerCase()}.
        </p>
      )}
    </aside>
  );
}

/* ------------------------------------------------------------------ мелочи */

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-xs font-semibold uppercase tracking-wider text-ink-400">
      {children}
    </h2>
  );
}

function Metric({
  label,
  value,
  mono,
  tone = 'normal',
}: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: 'normal' | 'warn';
}) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-ink-400">{label}</div>
      <div
        className={`text-lg font-bold tabular-nums ${mono ? 'font-mono text-sm' : ''} ${
          tone === 'warn' ? 'text-amber-600 dark:text-amber-400' : ''
        }`}
      >
        {value}
      </div>
    </div>
  );
}
