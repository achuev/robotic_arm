/**
 * Дословная типизация docs/api.md, контракт версии 1.
 * Имена полей и значения менять нельзя — на этом же документе стоит бэкенд.
 */

export const PROTOCOL_VERSION = 1;

/* ------------------------------------------------------------------ REST */

export type RobotStatus = 'idle' | 'moving' | 'error' | 'estop';

/** `GET /api/status` */
export interface StatusResponse {
  robot: RobotStatus;
  occupied: boolean;
  queue_length: number;
  estimated_wait_sec: number;
}

/** `POST /api/session` */
export interface SessionResponse {
  token: string;
}

export interface JointConfig {
  name: string;
  min: number;
  max: number;
  label: string;
  /**
   * Захват — единственный сустав, выраженный в долях 0..1, а не в радианах.
   * Пересчёт делает шлюз; клиент угла раскрытия не знает и знать не должен.
   */
  normalized?: boolean;
}

export interface Workspace {
  x: [number, number];
  y: [number, number];
  z: [number, number];
}

/** `GET /api/config` */
export interface AppConfig {
  protocol: number;
  joints: JointConfig[];
  workspace: Workspace;
  ee_jog_step_m: number;
  control_duration_sec: number;
  idle_timeout_sec: number;
  cooldown_sec: number;
  reconnect_grace_sec: number;
  /** Молчим дольше — сервер замораживает движение. Отсюда период `ping`. */
  watchdog_timeout_sec: number;
  /** Потолок входящих на сервере; считаются ВСЕ сообщения, включая `ping`. */
  max_msg_per_sec: number;
  max_queue: number;
  video_url: string;
  features: { cartesian: boolean; video: boolean };
}

/**
 * Значения по умолчанию из docs/api.md. Нужны только как страховка: если шлюз
 * не отдаст поле, клиент должен работать, а не падать на `undefined`.
 */
export const CONFIG_DEFAULTS = {
  cooldown_sec: 30,
  reconnect_grace_sec: 10,
  watchdog_timeout_sec: 3,
  max_msg_per_sec: 30,
  control_duration_sec: 90,
  idle_timeout_sec: 20,
  max_queue: 20,
  ee_jog_step_m: 0.01,
} as const;

/** Заполняет пропущенные поля значениями из контракта. */
export function withConfigDefaults(raw: Partial<AppConfig>): AppConfig {
  return {
    protocol: raw.protocol ?? PROTOCOL_VERSION,
    joints: raw.joints ?? [],
    workspace: raw.workspace ?? { x: [0, 0], y: [0, 0], z: [0, 0] },
    ee_jog_step_m: raw.ee_jog_step_m ?? CONFIG_DEFAULTS.ee_jog_step_m,
    control_duration_sec:
      raw.control_duration_sec ?? CONFIG_DEFAULTS.control_duration_sec,
    idle_timeout_sec: raw.idle_timeout_sec ?? CONFIG_DEFAULTS.idle_timeout_sec,
    cooldown_sec: raw.cooldown_sec ?? CONFIG_DEFAULTS.cooldown_sec,
    reconnect_grace_sec:
      raw.reconnect_grace_sec ?? CONFIG_DEFAULTS.reconnect_grace_sec,
    watchdog_timeout_sec:
      raw.watchdog_timeout_sec ?? CONFIG_DEFAULTS.watchdog_timeout_sec,
    max_msg_per_sec: raw.max_msg_per_sec ?? CONFIG_DEFAULTS.max_msg_per_sec,
    max_queue: raw.max_queue ?? CONFIG_DEFAULTS.max_queue,
    video_url: raw.video_url ?? '/video/stream',
    features: {
      cartesian: raw.features?.cartesian ?? false,
      video: raw.features?.video ?? true,
    },
  };
}

/** Имя сустава-захвата: он выносится из общего списка слайдеров в отдельный элемент. */
export const GRIPPER_JOINT = 'gripper';

/* -------------------------------------------------------- WS: клиент → сервер */

export interface HelloMsg {
  t: 'hello';
  token: string;
}
export interface EnqueueMsg {
  t: 'enqueue';
}
export interface SetJointMsg {
  t: 'set_joint';
  joint: string;
  /** Абсолютная цель в радианах, НЕ приращение. */
  position: number;
}
export interface SetJointsMsg {
  t: 'set_joints';
  positions: Record<string, number>;
}
export interface JogEeMsg {
  t: 'jog_ee';
  axis: 'x' | 'y' | 'z';
  /** Приращение в метрах. */
  delta: number;
}
export interface GripperMsg {
  t: 'gripper';
  /** 0 — закрыт, 1 — открыт. */
  value: number;
}
export interface PresetMsg {
  t: 'preset';
  name: PresetName;
}
export interface ReleaseMsg {
  t: 'release';
}
/** Выйти из очереди. От того, кто в ней не стоит, придёт `not_queued`. */
export interface LeaveMsg {
  t: 'leave';
}
export interface PingMsg {
  t: 'ping';
}

/**
 * `home` — одна поза, остальные — жесты из нескольких точек
 * (gateway/so101_gateway/gestures.py). Для клиента разницы нет: и то и другое
 * уходит одним сообщением `preset`.
 */
export type PresetName = 'home' | 'wave' | 'nod' | 'shake' | 'bow';

export type ClientMessage =
  | HelloMsg
  | EnqueueMsg
  | SetJointMsg
  | SetJointsMsg
  | JogEeMsg
  | GripperMsg
  | PresetMsg
  | ReleaseMsg
  | LeaveMsg
  | PingMsg;

/* -------------------------------------------------------- WS: сервер → клиент */

export interface StateMsg {
  t: 'state';
  joints: Record<string, number>;
  ee: { x: number; y: number; z: number };
  robot: RobotStatus;
  ts: number;
}
export interface QueueMsg {
  t: 'queue';
  /** `0` — управляет, `1+` — место в очереди, `-1` — наблюдатель вне очереди. */
  position: number;
  /** Сколько ходов должно завершиться до твоего, СЧИТАЯ текущего оператора. */
  ahead: number;
  eta_sec: number;
  /** Число ждущих; оператор в него не входит. */
  queue_length: number;
  you_control: boolean;
}

/** Позиция наблюдателя, не стоящего в очереди. */
export const POSITION_OBSERVER = -1;
export interface GrantedMsg {
  t: 'granted';
  /** null — ход без ограничения: в очереди никого нет. */
  duration_sec: number | null;
  /** UNIX-время, секунды, float. */
  expires_at: number | null;
}
export interface RevokedMsg {
  t: 'revoked';
  reason: RevokeReason;
}
export interface PongMsg {
  t: 'pong';
  ts: number;
}
export interface ErrorMsg {
  t: 'error';
  code: ErrorCode;
  message: string;
  /** `cooldown`: через сколько секунд снова можно встать в очередь. */
  retry_after_sec?: number;
  /** `out_of_range`: сустав, не принявший значение. Взаимоисключающе с `axis`. */
  joint?: string;
  /** `out_of_range`: ось джоггинга, упёршаяся в границу рабочей зоны. */
  axis?: string;
}

/**
 * Код закрытия для сокета, вытесненного вторым подключением с тем же токеном.
 *
 * ВНИМАНИЕ: в docs/api.md его нет — см. отчёт. Без такого сигнала две открытые
 * вкладки закрывают друг друга по кругу несколько раз в секунду, потому что
 * вытесненная вкладка немедленно переподключается и вытесняет вытеснившую.
 * Клиент дополнительно защищён детектором «мигания» в ConnectionManager,
 * но честное решение — согласованный код закрытия.
 */
export const CLOSE_DISPLACED = 4409;

export type RevokeReason =
  | 'timeout'
  | 'idle'
  | 'release'
  | 'admin'
  | 'estop'
  | 'disconnect';

export const REVOKE_REASONS: readonly RevokeReason[] = [
  'timeout',
  'idle',
  'release',
  'admin',
  'estop',
  'disconnect',
];

export type ErrorCode =
  | 'not_controller'
  | 'not_queued'
  | 'queue_full'
  | 'cooldown'
  | 'rate_limited'
  | 'out_of_range'
  | 'ik_failed'
  | 'estop'
  | 'bad_message';

export const ERROR_CODES: readonly ErrorCode[] = [
  'not_controller',
  'not_queued',
  'queue_full',
  'cooldown',
  'rate_limited',
  'out_of_range',
  'ik_failed',
  'estop',
  'bad_message',
];

export type ServerMessage =
  | StateMsg
  | QueueMsg
  | GrantedMsg
  | RevokedMsg
  | PongMsg
  | ErrorMsg;

/* ------------------------------------------------------------------ разбор */

/**
 * Разбирает входящий кадр. Неизвестный `t` возвращает null — контракт требует
 * игнорировать такие сообщения и НЕ рвать соединение (совместимость вперёд).
 */
export function parseServerMessage(raw: string): ServerMessage | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof data !== 'object' || data === null) return null;
  const t = (data as { t?: unknown }).t;
  if (typeof t !== 'string') return null;
  switch (t) {
    case 'state':
    case 'queue':
    case 'granted':
    case 'revoked':
    case 'pong':
    case 'error':
      return data as ServerMessage;
    default:
      return null;
  }
}

/** Ограничение цели пределами сустава — контракт клэмпит на сервере, дублируем локально. */
export function clamp(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) return min;
  return value < min ? min : value > max ? max : value;
}
