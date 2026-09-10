import { useCallback, useMemo, useState } from 'react';
import { Countdown, URGENT_SEC } from '../components/Countdown';
import { GripperControl } from '../components/GripperControl';
import { JogPad } from '../components/JogPad';
import { JointSlider } from '../components/JointSlider';
import {
  DisplacedCard,
  ExpiredCard,
  ObserverCard,
  QueueCard,
} from '../components/PhaseCards';
import {
  ConnectingSkeleton,
  EstopBanner,
  RobotChip,
} from '../components/StatusBits';
import { Toast } from '../components/Toast';
import { ArmView3D } from '../components/ArmView3D';
import { VideoStream } from '../components/VideoStream';
import { useGateway } from '../hooks/useGateway';
import { useNow } from '../hooks/useNow';
import { videoUrl } from '../lib/api';
import { canCommand, controlRemainingSec, isMotionStalled } from '../lib/machine';
import { GRIPPER_JOINT, type PresetName } from '../lib/protocol';
import {
  blockedDirections,
  describeWorkspacePosition,
  workspaceEdges,
  type Axis,
} from '../lib/workspace';

type Tab = 'joints' | 'point';

/** Что показываем сверху: поток с камеры или трёхмерную модель. */
type View = 'camera' | 'model';

export function Control() {
  const { boot, state, cm } = useGateway();
  const [tab, setTab] = useState<Tab>('joints');
  // Камера по умолчанию — но только если она вообще предусмотрена: настоящая
  // рука убедительнее модели. На стенде она выключена (features.video), и
  // тогда переключаться не из чего: модель занимает весь верх, вкладок нет.
  const [view, setView] = useState<View>('camera');
  const [videoForced] = useState(
    () => new URLSearchParams(window.location.search).get('video') === '1',
  );

  const controlling = state.phase === 'controlling';
  const now = useNow(250, controlling);
  const remaining = controlRemainingSec(state, now);
  const commandable = canCommand(state);

  const config = boot.status === 'ready' ? boot.config : null;
  const cartesian = config?.features.cartesian === true;
  // Пока конфиг не пришёл, камеры нет: иначе на секунду мигнёт пустая
  // рамка потока, которого не будет.
  //
  // ?video=1 включает камеру поверх флага — для панели на самом стенде, где
  // поток идёт по локальной сети и ничего не стоит. Снаружи этот параметр
  // бесполезен и потому безопасен: ретранслятор отдаёт на /video/* 404,
  // запрет стоит не здесь.
  const videoEnabled = videoForced || config?.features.video === true;
  const stalled = isMotionStalled(
    state,
    now,
    config?.watchdog_timeout_sec ?? 3
  );

  const sliderJoints = useMemo(
    () => config?.joints.filter((j) => j.name !== GRIPPER_JOINT) ?? [],
    [config]
  );
  const gripperJoint = config?.joints.find((j) => j.name === GRIPPER_JOINT) ?? null;

  const onJog = useCallback(
    (axis: 'x' | 'y' | 'z', delta: number) => cm?.jogEe(axis, delta),
    [cm]
  );

  /**
   * Где схват относительно объявленной рабочей зоны. Считаем сами, по `state.ee`
   * и `config.workspace`, не дожидаясь отказа сервера: на свежем стенде рука
   * включается ВНЕ зоны, и первый же джог законно приносит `out_of_range` —
   * человек не должен принять это за поломку.
   */
  const edges = useMemo(
    () => workspaceEdges(state.ee, config?.workspace),
    [state.ee, config]
  );
  const blocked = useMemo(() => blockedDirections(edges), [edges]);
  const zoneNotice = useMemo(
    () => describeWorkspacePosition(state.ee, config?.workspace),
    [state.ee, config]
  );
  // Ось из `out_of_range.axis`: сервер называет ту, что упёрлась на самом деле.
  const highlightAxis: Axis | null =
    state.highlight?.kind === 'axis'
      ? (state.highlight.name as Axis)
      : null;

  /**
   * Плашка про зону и тост про неё говорят одно и то же. Когда плашка на
   * экране, тост — шум: он закрывает джойстик, чтобы повторить прочитанное.
   * Гасим только показ: таймер тоста продолжает идти и вовремя снимет
   * подсветку оси.
   */
  const mutedToast =
    zoneNotice !== null &&
    state.toast?.code === 'out_of_range' &&
    highlightAxis !== null;

  if (boot.status === 'error') {
    return <FatalScreen message={boot.message} />;
  }

  if (boot.status === 'loading' || state.phase === 'connecting') {
    return (
      <div className="safe-top min-h-dvh">
        <ConnectingSkeleton />
      </div>
    );
  }

  const activeTab: Tab = cartesian ? tab : 'joints';

  return (
    /* lg: панель стенда. Экран ноутбука — не растянутый телефон: вид на
       руку остаётся на месте, а управление скроллится рядом. На узком
       всё как было, медиазапрос телефона не касается. */
    <div className="flex min-h-dvh flex-col overflow-x-hidden lg:h-dvh lg:overflow-hidden">
      {state.estop && <EstopBanner />}

      {/* Шапка: статус робота и, когда ход наш, таймер. */}
      <header className="safe-top safe-x sticky top-0 z-30 border-b border-ink-200 bg-ink-50/90 backdrop-blur dark:border-ink-800 dark:bg-ink-950/90">
        <div className="flex items-center justify-between gap-3 px-4 py-2">
          <RobotChip robot={state.robot} online={state.online} stalled={stalled} />
          {controlling && (
            <div className="flex items-center gap-2">
              {remaining === null ? (
                /* Ход без срока: за вами никого нет.
                   Знак вместо фразы и ровно того же размера, что кольцо
                   отсчёта: длинная надпись переносилась на две строки и
                   выдавливала статус робота из шапки на телефоне. */
                <span
                  title="Вы один — время не ограничено"
                  aria-label="Время не ограничено"
                  className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border-2 border-ink-200 text-2xl leading-none text-ink-400 dark:border-ink-700 dark:text-ink-500"
                >
                  ∞
                </span>
              ) : (
                <>
                  {remaining <= URGENT_SEC && (
                    <span className="text-sm font-semibold text-red-500">
                      Время заканчивается
                    </span>
                  )}
                  <Countdown
                    remainingSec={remaining}
                    totalSec={state.control?.durationSec ?? config?.control_duration_sec ?? 90}
                  />
                </>
              )}
            </div>
          )}
        </div>
      </header>

      {/* На телефоне вид сверху, управление под ним. На широком экране
          колонки меняются местами: ползунки слева, рука справа. Порядок в
          разметке при этом прежний — так на телефоне вид остаётся первым,
          а обход с клавиатуры не зависит от ширины. */}
      <div className="flex flex-1 flex-col lg:min-h-0 lg:flex-row-reverse">
        {/* На что смотрят. */}
        <div className="flex flex-col lg:min-h-0 lg:flex-1">
          {videoEnabled && view === 'camera' ? (
            <VideoStream src={config ? videoUrl(config) : null} enabled />
          ) : (
            <ArmView3D joints={state.joints} />
          )}

          {videoEnabled && (
            <div className="safe-x pt-2 lg:shrink-0 lg:px-3 lg:pb-3">
              <div className="grid grid-cols-2 gap-2 rounded-2xl bg-ink-100 p-1 dark:bg-ink-900">
                <TabButton active={view === 'camera'} onClick={() => setView('camera')}>
                  Камера
                </TabButton>
                <TabButton active={view === 'model'} onClick={() => setView('model')}>
                  Модель
                </TabButton>
              </div>
            </div>
          )}
        </div>

        {/* Чем управляют. */}
        <main className="safe-x flex-1 lg:w-[26rem] lg:min-h-0 lg:shrink-0 lg:overflow-y-auto lg:border-r lg:border-ink-200 lg:dark:border-ink-800">
        {state.phase === 'observer' && (
          <ObserverCard state={state} onEnqueue={() => cm?.enqueue()} />
        )}

        {state.phase === 'queued' && (
          <QueueCard state={state} onLeave={() => cm?.leaveQueue()} />
        )}

        {state.phase === 'expired' && (
          <ExpiredCard
            state={state}
            onEnqueue={() => cm?.enqueue()}
            onWatch={() => cm?.watchAgain()}
          />
        )}

        {state.phase === 'displaced' && (
          <DisplacedCard onResume={() => cm?.resume()} />
        )}

        {controlling && config && (
          <div className="flex flex-col gap-3 p-3">
            {cartesian && (
              <div className="grid grid-cols-2 gap-2 rounded-2xl bg-ink-100 p-1 dark:bg-ink-900">
                <TabButton active={activeTab === 'joints'} onClick={() => setTab('joints')}>
                  Суставы
                </TabButton>
                <TabButton active={activeTab === 'point'} onClick={() => setTab('point')}>
                  Точка
                </TabButton>
              </div>
            )}

            {activeTab === 'joints' ? (
              <div className="flex flex-col gap-2">
                {sliderJoints.map((joint) => (
                  <JointSlider
                    key={joint.name}
                    joint={joint}
                    actual={state.joints[joint.name]}
                    disabled={!commandable}
                    highlighted={
                      state.highlight?.kind === 'joint' &&
                      state.highlight.name === joint.name
                    }
                    onChange={(v) => cm?.setJoint(joint.name, v)}
                  />
                ))}
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {zoneNotice && <ZoneNotice notice={zoneNotice} />}
                <div className="flex gap-3">
                  <JogPad
                    mode="xy"
                    disabled={!commandable}
                    step={config.ee_jog_step_m}
                    onJog={onJog}
                    highlightAxis={highlightAxis}
                    blocked={blocked}
                    labels={{
                      up: 'вперёд',
                      down: 'назад',
                      left: 'влево',
                      right: 'вправо',
                    }}
                  />
                  <JogPad
                    mode="z"
                    disabled={!commandable}
                    step={config.ee_jog_step_m}
                    onJog={onJog}
                    highlightAxis={highlightAxis}
                    blocked={blocked}
                    labels={{ up: 'выше', down: 'ниже' }}
                  />
                </div>
              </div>
            )}

            {gripperJoint && (
              <GripperControl
                joint={gripperJoint}
                actual={state.joints[gripperJoint.name]}
                disabled={!commandable}
                onChange={(v) => cm?.gripper(v)}
              />
            )}

            <Presets
              disabled={!commandable}
              onPreset={(name) => cm?.preset(name)}
            />

            <button
              type="button"
              onClick={() => cm?.release()}
              className="h-14 rounded-xl border-2 border-ink-300 text-base font-semibold text-ink-500 transition active:scale-[0.98] dark:border-ink-700 dark:text-ink-400"
            >
              Передать следующему
            </button>
          </div>
        )}
        </main>
      </div>

      <div className="safe-bottom" />
      <Toast
        toast={state.toast}
        muted={mutedToast}
        onDismiss={() => cm?.dismissToast()}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ мелочи */

/**
 * Спокойная плашка про край рабочей зоны.
 *
 * Осознанно НЕ красная и без слова «ошибка»: рука у границы — это штатное
 * положение, а на свежем стенде ещё и стартовое. Задача текста — снять испуг
 * до того, как сервер пришлёт свой законный `out_of_range`.
 */
function ZoneNotice({
  notice,
}: {
  notice: { kind: string; title: string; hint: string };
}) {
  return (
    <div className="flex items-start gap-3 rounded-2xl bg-amber-100/70 px-4 py-3 dark:bg-amber-500/10">
      <span
        aria-hidden
        className="mt-0.5 h-5 w-5 shrink-0 rounded-full border-2 border-amber-500"
      />
      <div>
        <div className="text-[15px] font-semibold text-amber-900 dark:text-amber-200">
          {notice.title}
        </div>
        <div className="mt-0.5 text-sm text-amber-900/80 dark:text-amber-200/80">
          {notice.hint}
        </div>
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`h-12 rounded-xl text-base font-semibold transition ${
        active
          ? 'bg-white shadow-sm dark:bg-ink-800'
          : 'text-ink-500 dark:text-ink-400'
      }`}
    >
      {children}
    </button>
  );
}

/** Подписи кнопок. Порядок — от самого понятного к остальным. */
const PRESET_BUTTONS: { name: PresetName; label: string; hint: string }[] = [
  { name: 'wave', label: 'Помахать', hint: '👋' },
  { name: 'nod', label: 'Кивнуть', hint: '🙂' },
  { name: 'shake', label: 'Помотать', hint: '🙃' },
  { name: 'bow', label: 'Поклон', hint: '🙇' },
  { name: 'home', label: 'Домой', hint: '🏠' },
];

function Presets({
  disabled,
  onPreset,
}: {
  disabled: boolean;
  onPreset: (name: PresetName) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-3">
      {PRESET_BUTTONS.map(({ name, label, hint }) => (
        <button
          key={name}
          type="button"
          disabled={disabled}
          onClick={() => onPreset(name)}
          // «Домой» — нечётная пятая кнопка; растягиваем её на всю ширину,
          // чтобы сетка не заканчивалась дырой.
          className={`grab h-16 rounded-xl border-2 border-ink-300 text-lg font-semibold transition active:scale-[0.97] disabled:opacity-40 dark:border-ink-700 ${
            name === 'home' ? 'col-span-2' : ''
          }`}
        >
          <span className="mr-2" aria-hidden>
            {hint}
          </span>
          {label}
        </button>
      ))}
    </div>
  );
}

function FatalScreen({ message }: { message: string }) {
  return (
    <div className="safe-x flex min-h-dvh flex-col items-center justify-center gap-5 p-8 text-center">
      <div className="flex h-20 w-20 items-center justify-center rounded-full bg-ink-100 dark:bg-ink-800">
        <svg viewBox="0 0 48 48" className="h-10 w-10 text-ink-400" aria-hidden>
          <path d="M24 10v20" className="stroke-current" strokeWidth="4" strokeLinecap="round" />
          <circle cx="24" cy="38" r="2.5" className="fill-current" />
        </svg>
      </div>
      <div>
        <h1 className="text-2xl font-bold">{message}</h1>
        <p className="mt-2 text-ink-500 dark:text-ink-400">
          Проверьте, что телефон в той же сети, и попробуйте обновить страницу.
        </p>
      </div>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="h-16 w-full max-w-xs rounded-2xl bg-accent-500 text-lg font-bold text-white"
      >
        Обновить
      </button>
    </div>
  );
}
