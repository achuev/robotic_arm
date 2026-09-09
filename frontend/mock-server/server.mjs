import http from 'node:http';
import { randomUUID } from 'node:crypto';
import { WebSocketServer } from 'ws';
import { renderFrame } from './scene.mjs';
import {
  HOME,
  JOINTS,
  RobotModel,
  WAVE_SCRIPT,
  WORKSPACE,
  ZERO_POSE,
  clamp,
  jointSpec,
} from './robot.mjs';

/**
 * Мок шлюза so101_gateway по docs/api.md, контракт версии 1.
 * Ничего не знает ни про ROS, ни про Python — только про контракт.
 *
 * Запуск:   npm run mock
 * Панель:   http://localhost:8080/mock  — оттуда добавляются боты,
 *           чтобы увидеть экран очереди без второго телефона.
 */

/* ------------------------------------------------------------------ параметры */

const PORT = Number(process.env.PORT ?? 8080);
const ADMIN_TOKEN = process.env.ADMIN_TOKEN ?? 'dev-admin';

const CONTROL_DURATION_SEC = Number(process.env.CONTROL_DURATION_SEC ?? 90);
const IDLE_TIMEOUT_SEC = Number(process.env.IDLE_TIMEOUT_SEC ?? 20);
const RECONNECT_GRACE_SEC = Number(process.env.RECONNECT_GRACE_SEC ?? 10);
/** Таймаут ожидания ухода в home, а НЕ гарантия прибытия — контракт §4. */
const HANDOVER_HOME_SEC = 2;
const COOLDOWN_SEC = Number(process.env.COOLDOWN_SEC ?? 30);
/** Молчит дольше — движение замирает. Следит за живостью, не за командами. */
const WATCHDOG_TIMEOUT_SEC = Number(process.env.WATCHDOG_TIMEOUT_SEC ?? 3);
/** Общее число участников, ВКЛЮЧАЯ оператора. */
const MAX_QUEUE = Number(process.env.MAX_QUEUE ?? 20);
const MAX_MSG_PER_SEC = Number(process.env.MAX_MSG_PER_SEC ?? 30);
/** Код закрытия для вытесненного сокета. В контракте его нет — см. отчёт. */
const CLOSE_DISPLACED = 4409;
const FEATURE_CARTESIAN = process.env.MOCK_CARTESIAN !== '0';
/**
 * Стартовать в нулевой позе, как настоящий стенд после включения. Она лежит
 * ВНЕ рабочей зоны, и первый джог законно приносит `out_of_range` — ровно тот
 * случай, ради которого на экране управления появилась плашка «рука у края».
 */
const START_ZERO_POSE = process.env.MOCK_ZERO_POSE === '1';
const FEATURE_VIDEO = process.env.MOCK_VIDEO !== '0';
const TICK_HZ = 10;

const now = () => Date.now();
const unix = () => Date.now() / 1000;

/* ------------------------------------------------------------------ состояние */

const robotModel = new RobotModel();

/** @type {Map<string, Client>} token → клиент */
const clients = new Map();
/** @type {string[]} токены в очереди, БЕЗ текущего оператора */
const queue = [];

let controller = null; // token
let controlExpiresAt = 0; // мс
let lastCommandAt = 0; // мс
let handoverUntil = 0; // мс, рука едет в home между операторами
let hadController = false;
let estop = false;
let waveUntil = 0;
let waveStartedAt = 0;
let frameIndex = 0;

class Client {
  constructor(token, { bot = false } = {}) {
    this.token = token;
    this.bot = bot;
    this.ws = null;
    this.disconnectedAt = bot ? 0 : now();
    this.cooldownUntil = 0;
    this.msgTimes = [];
    this.lastSentQueue = null;
    /** Когда от клиента последний раз пришло ХОТЬ ЧТО-ТО, включая ping. */
    this.lastSeenAt = now();
  }
  get connected() {
    return this.bot || (this.ws && this.ws.readyState === 1);
  }
}

function getOrCreateClient(token) {
  let c = clients.get(token);
  if (!c) {
    c = new Client(token);
    clients.set(token, c);
  }
  return c;
}

function send(client, msg) {
  if (client.bot || !client.ws || client.ws.readyState !== 1) return;
  try {
    client.ws.send(JSON.stringify(msg));
  } catch {
    /* закрылся */
  }
}

function sendError(client, code, message, extra = {}) {
  send(client, { t: 'error', code, message, ...extra });
}

function broadcast(msg) {
  for (const c of clients.values()) send(c, msg);
}

/* ------------------------------------------------------------------ очередь */

function positionOf(token) {
  if (controller === token) return 0;
  const i = queue.indexOf(token);
  // -1 — наблюдатель вне очереди (контракт §3).
  return i === -1 ? -1 : i + 1;
}

function controllerRemainingSec() {
  if (!controller) return 0;
  return Math.max(0, Math.round((controlExpiresAt - now()) / 1000));
}

/** Сколько ходов должно завершиться до твоего, СЧИТАЯ текущего оператора. */
function aheadFor(position) {
  if (position <= 0) return 0;
  return position - 1 + (controller ? 1 : 0);
}

/** eta_sec = ahead * (CONTROL_DURATION + HANDOVER_HOME) — честная оценка сверху. */
function etaFor(position) {
  return aheadFor(position) * (CONTROL_DURATION_SEC + HANDOVER_HOME_SEC);
}

/** Число участников, включая оператора — именно это ограничивает max_queue. */
function participants() {
  return queue.length + (controller ? 1 : 0);
}

function queueMsgFor(token) {
  const position = positionOf(token);
  return {
    t: 'queue',
    position,
    ahead: aheadFor(position),
    eta_sec: etaFor(position),
    // queue_length — только ждущие, оператор не входит.
    queue_length: queue.length,
    you_control: controller === token,
  };
}

/** Рассылает персональные `queue` — но только тем, у кого что-то изменилось. */
function pushQueueUpdates(force = false) {
  for (const c of clients.values()) {
    if (c.bot) continue;
    const msg = queueMsgFor(c.token);
    const key = `${msg.position}|${msg.queue_length}|${msg.you_control}|${
      Math.round(msg.eta_sec / 5)
    }`;
    if (!force && c.lastSentQueue === key) continue;
    c.lastSentQueue = key;
    send(c, msg);
  }
}

function startCooldown(token) {
  const c = clients.get(token);
  if (c) c.cooldownUntil = now() + COOLDOWN_SEC * 1000;
}

function revokeControl(reason) {
  if (!controller) return;
  const token = controller;
  const c = clients.get(token);
  controller = null;
  controlExpiresAt = 0;
  waveUntil = 0;

  if (c) {
    send(c, { t: 'revoked', reason });
    // Контракт §1: после своего хода 30 с нельзя вставать в очередь.
    if (reason === 'timeout' || reason === 'idle' || reason === 'release') {
      startCooldown(token);
    }
  }

  // Между операторами рука уезжает в home (HANDOVER_HOME, 2 с).
  handoverUntil = now() + HANDOVER_HOME_SEC * 1000;
  robotModel.goHome();
  pushQueueUpdates(true);
}

function maybeGrant() {
  if (estop || controller || queue.length === 0) return;
  if (hadController && now() < handoverUntil) return;

  const token = queue.shift();
  const c = clients.get(token);
  if (!c || !c.connected) {
    // Отвалился, пока ждал — просто пропускаем.
    if (c) clients.delete(token);
    return maybeGrant();
  }

  controller = token;
  hadController = true;
  controlExpiresAt = now() + CONTROL_DURATION_SEC * 1000;
  lastCommandAt = now();
  send(c, {
    t: 'granted',
    duration_sec: CONTROL_DURATION_SEC,
    expires_at: controlExpiresAt / 1000,
  });
  pushQueueUpdates(true);
  log(`ход у ${short(token)}${c.bot ? ' (бот)' : ''}, очередь ${queue.length}`);
}

function dropClient(token, { immediate }) {
  const c = clients.get(token);
  if (!c) return;
  if (controller === token) {
    if (immediate) revokeControl('disconnect');
    return; // иначе ждём RECONNECT_GRACE
  }
  const i = queue.indexOf(token);
  if (i !== -1) queue.splice(i, 1);
  clients.delete(token);
  pushQueueUpdates();
}

/* ------------------------------------------------------------------ команды */

function noteCommand() {
  lastCommandAt = now();
}

/** Считаются ВСЕ входящие, включая `ping` и `hello` — контракт §3. */
function rateLimited(client) {
  const t = now();
  client.msgTimes = client.msgTimes.filter((x) => t - x < 1000);
  client.msgTimes.push(t);
  return client.msgTimes.length > MAX_MSG_PER_SEC;
}

function requireController(client) {
  if (controller !== client.token) {
    sendError(client, 'not_controller', 'Роботом сейчас управляет другой человек');
    return false;
  }
  if (estop) {
    sendError(client, 'estop', 'Активна аварийная остановка');
    return false;
  }
  if (now() < handoverUntil) {
    sendError(client, 'not_controller', 'Рука возвращается в исходное положение');
    return false;
  }
  return true;
}

function handleMessage(client, raw) {
  // Любое входящее сообщение — признак живого клиента, watchdog кормится им.
  client.lastSeenAt = now();

  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    sendError(client, 'bad_message', 'Некорректный JSON');
    return;
  }
  if (!msg || typeof msg.t !== 'string') {
    sendError(client, 'bad_message', 'Нет поля t');
    return;
  }

  // Лимит считает ВСЕ входящие, включая ping, — поэтому проверка идёт до него.
  if (rateLimited(client)) {
    sendError(client, 'rate_limited', 'Слишком часто');
    return;
  }

  if (msg.t === 'ping') {
    send(client, { t: 'pong', ts: unix() });
    return;
  }

  switch (msg.t) {
    case 'hello':
      // Уже обработан при апгрейде; повторный hello просто освежает состояние.
      pushQueueUpdates(true);
      return;

    case 'enqueue': {
      if (estop) return sendError(client, 'estop', 'Робот остановлен оператором');
      if (controller === client.token || queue.includes(client.token)) {
        return pushQueueUpdates(true);
      }
      const left = Math.ceil((client.cooldownUntil - now()) / 1000);
      if (left > 0) {
        return sendError(
          client,
          'cooldown',
          `Дайте другим попробовать, ещё ${left} с`,
          { retry_after_sec: left }
        );
      }
      // max_queue ограничивает участников ВМЕСТЕ с оператором — контракт §4.
      if (participants() >= MAX_QUEUE) {
        return sendError(client, 'queue_full', 'Слишком много желающих, зайдите позже');
      }
      queue.push(client.token);
      log(`${short(client.token)} встал в очередь, длина ${queue.length}`);
      pushQueueUpdates(true);
      maybeGrant();
      return;
    }

    case 'set_joint': {
      if (!requireController(client)) return;
      noteCommand();
      const result = robotModel.setTarget(msg.joint, Number(msg.position));
      if (result === 'unknown_joint') {
        return sendError(client, 'bad_message', `Нет сустава ${msg.joint}`);
      }
      if (result === 'out_of_range') {
        // Контракт: ошибка обязана назвать орган — клиент не должен гадать.
        return sendError(client, 'out_of_range', 'Значение зажато в предел сустава', {
          joint: msg.joint,
        });
      }
      return;
    }

    case 'set_joints': {
      if (!requireController(client)) return;
      noteCommand();
      let clampedJoint = null;
      for (const [name, value] of Object.entries(msg.positions ?? {})) {
        if (robotModel.setTarget(name, Number(value)) === 'out_of_range') {
          clampedJoint = clampedJoint ?? name;
        }
      }
      if (clampedJoint) {
        sendError(client, 'out_of_range', 'Часть значений зажата в пределы', {
          joint: clampedJoint,
        });
      }
      return;
    }

    case 'jog_ee': {
      if (!requireController(client)) return;
      noteCommand();
      if (!FEATURE_CARTESIAN) {
        return sendError(client, 'ik_failed', 'Декартово управление недоступно');
      }
      const axis = msg.axis;
      if (!['x', 'y', 'z'].includes(axis)) {
        return sendError(client, 'bad_message', 'Неизвестная ось');
      }
      const target = robotModel.targetEe();
      target[axis] += Number(msg.delta) || 0;
      // Границы зоны проверяются ПЕРВЫМИ — контракт §3.
      const result = robotModel.moveEeTo(target);
      if (result.status === 'out_of_range') {
        // Ось берём ту, что упёрлась НА САМОМ ДЕЛЕ: она не обязана совпадать
        // с той, по которой джогали (рука могла стоять у другой границы).
        return sendError(client, 'out_of_range', 'Точка подрезана в рабочую зону', {
          axis: result.axis ?? axis,
        });
      }
      if (result.status === 'ik_failed') {
        return sendError(client, 'ik_failed', 'Сюда рука не дотянется');
      }
      return;
    }

    case 'gripper': {
      if (!requireController(client)) return;
      noteCommand();
      robotModel.setTarget('gripper', clamp(Number(msg.value), 0, 1));
      return;
    }

    case 'preset': {
      if (!requireController(client)) return;
      noteCommand();
      // Жесты (`gateway/so101_gateway/gestures.py`) мок не воспроизводит
      // покадрово — он для проверки UI, а не движения. Достаточно, чтобы
      // кнопки не отвечали `bad_message`: иначе на моке нельзя проверить
      // ни раскладку кнопок, ни блокировку их в чужой ход.
      const GESTURES = ['wave', 'nod', 'shake', 'bow'];
      if (msg.name === 'home') {
        robotModel.goHome();
        waveUntil = 0;
      } else if (GESTURES.includes(msg.name)) {
        waveStartedAt = now();
        waveUntil = now() + 3400;
      } else {
        sendError(client, 'bad_message', 'Неизвестный пресет');
      }
      return;
    }

    case 'release':
      if (controller !== client.token) {
        return sendError(client, 'not_controller', 'Вы и так не управляете');
      }
      revokeControl('release');
      maybeGrant();
      return;

    case 'leave': {
      const i = queue.indexOf(client.token);
      if (i === -1) {
        return sendError(client, 'not_queued', 'Вы не стоите в очереди');
      }
      queue.splice(i, 1);
      log(`${short(client.token)} вышел из очереди, осталось ${queue.length}`);
      pushQueueUpdates(true);
      return;
    }

    default:
      // Неизвестный t игнорируется, соединение не рвём — контракт §3.
      return;
  }
}

/* ------------------------------------------------------------------ такт 10 Гц */

let lastTick = now();

function tick() {
  const t = now();
  const dt = Math.min(0.25, (t - lastTick) / 1000);
  lastTick = t;

  if (waveUntil && t < waveUntil) {
    const elapsed = (t - waveStartedAt) / 1000;
    for (const frame of WAVE_SCRIPT) {
      if (elapsed >= frame.at) {
        for (const [name, value] of Object.entries(frame.joints)) {
          robotModel.target[name] = value;
        }
      }
    }
  } else if (waveUntil && t >= waveUntil) {
    waveUntil = 0;
    robotModel.goHome();
  }

  // Бот у руля шевелит рукой, чтобы поток state выглядел живым.
  const ctrlClient = controller ? clients.get(controller) : null;
  if (ctrlClient?.bot && !estop) {
    const phase = t / 1000;
    robotModel.target.shoulder_pan = Math.sin(phase * 0.6) * 1.1;
    robotModel.target.shoulder_lift = 0.3 + Math.sin(phase * 0.9) * 0.5;
    robotModel.target.elbow_flex = -0.6 + Math.cos(phase * 0.7) * 0.6;
    robotModel.target.gripper = 0.5 + Math.sin(phase * 1.4) * 0.5;
    lastCommandAt = t;
  }

  // Watchdog: клиент молчит дольше таймаута — движение замирает на месте.
  // Следит за ЖИВОСТЬЮ соединения, а не за потоком команд: ping его кормит.
  // Цель при этом никуда не девается — как только клиент отзовётся, рука
  // продолжит доезжать туда же, повторять команду не нужно.
  const ctrl = controller ? clients.get(controller) : null;
  const frozen =
    !!ctrl && !ctrl.bot && t - ctrl.lastSeenAt > WATCHDOG_TIMEOUT_SEC * 1000;

  const moving = estop || frozen ? false : robotModel.step(dt);

  let robot = 'idle';
  if (estop) robot = 'estop';
  else if (moving || t < handoverUntil) robot = 'moving';

  if (!estop) {
    if (controller && t >= controlExpiresAt) {
      log(`время вышло у ${short(controller)}`);
      revokeControl('timeout');
    } else if (controller && t - lastCommandAt >= IDLE_TIMEOUT_SEC * 1000) {
      log(`бездействие у ${short(controller)}`);
      revokeControl('idle');
    }

    // Оператор в обрыве: контракт даёт RECONNECT_GRACE на возврат.
    if (controller) {
      const c = clients.get(controller);
      if (c && !c.connected && t - c.disconnectedAt > RECONNECT_GRACE_SEC * 1000) {
        log(`${short(controller)} не вернулся за 10 с`);
        const token = controller;
        revokeControl('disconnect');
        clients.delete(token);
      }
    }

    maybeGrant();
  }

  broadcast({
    t: 'state',
    joints: { ...robotModel.actual },
    ee: robotModel.ee(),
    robot,
    ts: unix(),
  });

  pushQueueUpdates();
  currentRobotStatus = robot;
}

let currentRobotStatus = 'idle';
setInterval(tick, 1000 / TICK_HZ);

/* ------------------------------------------------------------------ HTTP */

function json(res, code, body, headers = {}) {
  const payload = JSON.stringify(body);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type, X-Admin-Token',
    ...headers,
  });
  res.end(payload);
}

function readCookie(req, name) {
  const raw = req.headers.cookie ?? '';
  for (const part of raw.split(';')) {
    const [k, ...v] = part.trim().split('=');
    if (k === name) return decodeURIComponent(v.join('='));
  }
  return null;
}

function isAdmin(req) {
  return req.headers['x-admin-token'] === ADMIN_TOKEN;
}

/** Тело `GET /api/admin/queue` — один в один со снимком настоящего шлюза. */
function adminSnapshot() {
  const t = now();
  const ctrl = controller ? clients.get(controller) : null;
  return {
    now: unix(),
    robot: currentRobotStatus,
    estop,
    handover_until: handoverUntil ? handoverUntil / 1000 : null,
    controller: controller
      ? {
          token: controller,
          connected: ctrl ? ctrl.connected : false,
          expires_at: controlExpiresAt / 1000,
          remaining_sec: Math.max(0, (controlExpiresAt - t) / 1000),
          idle_for_sec: Math.max(0, (t - lastCommandAt) / 1000),
          disconnected_for_sec:
            ctrl && !ctrl.connected && ctrl.disconnectedAt
              ? (t - ctrl.disconnectedAt) / 1000
              : null,
        }
      : null,
    queue: queue.map((token, i) => ({
      token,
      position: i + 1,
      eta_sec: etaFor(i + 1),
      connected: clients.get(token)?.connected ?? false,
    })),
    observers: [...clients.keys()].filter(
      (token) => token !== controller && !queue.includes(token)
    ),
    cooldowns: Object.fromEntries(
      [...clients.entries()]
        .filter(([, c]) => c.cooldownUntil > t)
        .map(([token, c]) => [token, Math.round((c.cooldownUntil - t) / 10) / 100])
    ),
  };
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host ?? 'localhost'}`);
  const path = url.pathname;

  if (req.method === 'OPTIONS') return json(res, 204, {});

  /* ---- контракт §2 ---- */

  if (path === '/api/health') {
    return json(res, 200, { ok: true });
  }

  if (path === '/api/status') {
    return json(res, 200, {
      robot: currentRobotStatus,
      occupied: controller !== null,
      // Только ждущие, оператор не входит.
      queue_length: queue.length,
      // Ожидание для того, кто встанет в очередь СЛЕДУЮЩИМ.
      estimated_wait_sec: etaFor(queue.length + 1),
    });
  }

  if (path === '/api/session' && req.method === 'POST') {
    const existing = readCookie(req, 'so101_token');
    const token = existing && clients.has(existing) ? existing : randomUUID();
    getOrCreateClient(token);
    return json(res, 200, { token }, {
      'Set-Cookie': `so101_token=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400`,
    });
  }

  if (path === '/api/config') {
    return json(res, 200, {
      protocol: 1,
      joints: JOINTS.map(({ name, min, max, label, normalized }) =>
        // Захват всюду в долях 0..1 и помечен normalized — контракт §2.
        normalized ? { name, min, max, label, normalized } : { name, min, max, label }
      ),
      workspace: WORKSPACE,
      ee_jog_step_m: 0.01,
      control_duration_sec: CONTROL_DURATION_SEC,
      idle_timeout_sec: IDLE_TIMEOUT_SEC,
      cooldown_sec: COOLDOWN_SEC,
      reconnect_grace_sec: RECONNECT_GRACE_SEC,
      watchdog_timeout_sec: WATCHDOG_TIMEOUT_SEC,
      max_msg_per_sec: MAX_MSG_PER_SEC,
      max_queue: MAX_QUEUE,
      video_url: '/video/stream',
      features: { cartesian: FEATURE_CARTESIAN, video: FEATURE_VIDEO },
    });
  }

  /* ---- админские: без валидного токена 403 и никаких подробностей ---- */

  if (path.startsWith('/api/admin/')) {
    if (!isAdmin(req)) return json(res, 403, {});
    if (path === '/api/admin/queue') {
      // Форма тела повторяет so101_gateway `SessionManager.admin_snapshot()`.
      // В docs/api.md её нет (см. отчёт), а панель отлаживают именно здесь —
      // разойтись со шлюзом означало бы отлаживать несуществующий экран.
      return json(res, 200, adminSnapshot());
    }
    if (path === '/api/admin/kick' && req.method === 'POST') {
      revokeControl('admin');
      maybeGrant();
      return json(res, 200, { ok: true });
    }
    if (path === '/api/admin/estop' && req.method === 'POST') {
      estop = true;
      revokeControl('estop');
      return json(res, 200, { ok: true, estop: true });
    }
    if (path === '/api/admin/release_estop' && req.method === 'POST') {
      estop = false;
      handoverUntil = 0;
      maybeGrant();
      return json(res, 200, { ok: true, estop: false });
    }
    return json(res, 403, {});
  }

  /* ---- видео: MJPEG-подобный multipart-поток ---- */

  if (path === '/video/stream') {
    if (!FEATURE_VIDEO) {
      res.writeHead(503);
      return res.end('video disabled');
    }
    return streamVideo(res);
  }

  /* ---- панель управления моком (в реальном шлюзе такого нет) ---- */

  if (path === '/mock' || path === '/mock/') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    return res.end(PANEL_HTML);
  }
  if (path.startsWith('/mock/')) return handleMockControl(path, url, res);

  res.writeHead(404);
  res.end('not found');
});

/* ------------------------------------------------------------------ видео */

const videoClients = new Set();

function streamVideo(res) {
  res.writeHead(200, {
    'Content-Type': 'multipart/x-mixed-replace; boundary=so101frame',
    'Cache-Control': 'no-store, no-cache, must-revalidate',
    Connection: 'close',
    Pragma: 'no-cache',
  });
  videoClients.add(res);
  res.on('close', () => videoClients.delete(res));
}

setInterval(() => {
  if (videoClients.size === 0) return;
  frameIndex++;
  const png = renderFrame(robotModel.actual, currentRobotStatus, frameIndex);
  const head = Buffer.from(
    `--so101frame\r\nContent-Type: image/png\r\nContent-Length: ${png.length}\r\n\r\n`
  );
  for (const res of videoClients) {
    try {
      res.write(head);
      res.write(png);
      res.write(Buffer.from('\r\n'));
    } catch {
      videoClients.delete(res);
    }
  }
}, 100);

/* ------------------------------------------------------------------ боты */

let botSeq = 0;

function addBot() {
  const token = `bot-${++botSeq}-${randomUUID().slice(0, 8)}`;
  const c = new Client(token, { bot: true });
  clients.set(token, c);
  queue.push(token);
  log(`добавлен бот ${short(token)}, очередь ${queue.length}`);
  pushQueueUpdates(true);
  maybeGrant();
  return token;
}

function removeBot() {
  // Сначала осиротевшие: бот, у которого estop отобрал ход, остаётся
  // наблюдателем — не в очереди и не у руля, но всё ещё в clients.
  for (const [token, c] of clients) {
    if (c.bot && token !== controller && !queue.includes(token)) {
      clients.delete(token);
      return true;
    }
  }
  // Затем ждущих в очереди, в последнюю очередь — того, кто у руля.
  for (let i = queue.length - 1; i >= 0; i--) {
    const c = clients.get(queue[i]);
    if (c?.bot) {
      queue.splice(i, 1);
      clients.delete(c.token);
      pushQueueUpdates(true);
      return true;
    }
  }
  if (controller && clients.get(controller)?.bot) {
    const token = controller;
    revokeControl('admin');
    clients.delete(token);
    maybeGrant();
    return true;
  }
  return false;
}

function botCount() {
  let n = 0;
  for (const c of clients.values()) if (c.bot) n++;
  return n;
}

function handleMockControl(path, url, res) {
  switch (path) {
    case '/mock/bots/add':
      addBot();
      break;
    case '/mock/bots/remove':
      removeBot();
      break;
    case '/mock/bots/set': {
      const target = Number(url.searchParams.get('n') ?? 0);
      while (botCount() < target) addBot();
      while (botCount() > target) if (!removeBot()) break;
      break;
    }
    case '/mock/kick':
      revokeControl('admin');
      maybeGrant();
      break;
    case '/mock/estop':
      estop = true;
      revokeControl('estop');
      break;
    case '/mock/release_estop':
      estop = false;
      handoverUntil = 0;
      maybeGrant();
      break;
    case '/mock/zero':
      // Нулевая поза: рука вытянута и стоит ВНЕ рабочей зоны.
      robotModel.snapTo(ZERO_POSE);
      break;
    case '/mock/reset':
      for (const [token, c] of clients) if (c.bot) clients.delete(token);
      queue.length = 0;
      controller = null;
      estop = false;
      handoverUntil = 0;
      hadController = false;
      for (const c of clients.values()) c.cooldownUntil = 0;
      robotModel.goHome();
      pushQueueUpdates(true);
      break;
    case '/mock/state':
      break;
    default:
      res.writeHead(404);
      return res.end('unknown mock command');
  }

  return json(res, 200, {
    bots: botCount(),
    controller: controller ? short(controller) : null,
    controller_is_bot: controller ? (clients.get(controller)?.bot ?? false) : false,
    queue_length: queue.length,
    remaining_sec: controllerRemainingSec(),
    estop,
    clients: clients.size,
  });
}

/* ------------------------------------------------------------------ WebSocket */

const wss = new WebSocketServer({ server, path: '/ws' });

wss.on('connection', (ws, req) => {
  let client = null;
  let helloTimer = setTimeout(() => {
    // Контракт: `hello` обязателен первым сообщением.
    if (!client) {
      try {
        ws.send(JSON.stringify({ t: 'error', code: 'bad_message', message: 'Нет hello' }));
      } catch { /* уже закрыт */ }
      ws.close();
    }
  }, 5000);

  ws.on('message', (data) => {
    const raw = data.toString();

    if (!client) {
      let msg;
      try {
        msg = JSON.parse(raw);
      } catch {
        ws.send(JSON.stringify({ t: 'error', code: 'bad_message', message: 'Некорректный JSON' }));
        return;
      }
      if (msg?.t !== 'hello' || typeof msg.token !== 'string' || !msg.token) {
        ws.send(JSON.stringify({ t: 'error', code: 'bad_message', message: 'Первым сообщением нужен hello с token' }));
        ws.close();
        return;
      }

      clearTimeout(helloTimer);
      client = getOrCreateClient(msg.token);
      // Второе подключение с тем же токеном вытесняет первое; ход и место
      // в очереди остаются за токеном (контракт §4). Закрываем спец-кодом,
      // иначе вытесненная вкладка переподключится и вытеснит эту в ответ.
      if (client.ws && client.ws !== ws && client.ws.readyState === 1) {
        try {
          client.ws.close(CLOSE_DISPLACED, 'displaced by newer connection');
        } catch { /* ignore */ }
        log(`${short(msg.token)} вытеснен новой вкладкой`);
      }
      client.ws = ws;
      client.disconnectedAt = 0;
      client.lastSeenAt = now();
      client.lastSentQueue = null;
      log(`${short(client.token)} подключился${controller === client.token ? ' (вернул ход)' : ''}`);

      send(client, queueMsgFor(client.token));
      if (controller === client.token) {
        send(client, {
          t: 'granted',
          duration_sec: CONTROL_DURATION_SEC,
          expires_at: controlExpiresAt / 1000,
        });
      }
      if (estop) sendError(client, 'estop', 'Робот остановлен оператором');
      return;
    }

    handleMessage(client, raw);
  });

  ws.on('close', () => {
    clearTimeout(helloTimer);
    if (!client || client.ws !== ws) return;
    client.ws = null;
    client.disconnectedAt = now();
    log(`${short(client.token)} отключился`);
    // Оператору даём RECONNECT_GRACE, остальные выбывают немедленно.
    dropClient(client.token, { immediate: false });
  });

  ws.on('error', () => { /* close придёт следом */ });
});

/* ------------------------------------------------------------------ панель */

const PANEL_HTML = `<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Мок-шлюз SO-101</title>
<style>
:root{color-scheme:light dark}
body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:24px;max-width:640px}
h1{font-size:20px;margin:0 0 4px}
p.sub{margin:0 0 20px;opacity:.7;font-size:14px}
button{font:inherit;padding:12px 16px;margin:0 8px 8px 0;border-radius:10px;
  border:1px solid #8884;background:#8881;cursor:pointer}
button:hover{background:#8883}
button.danger{border-color:#e34;color:#e34}
pre{background:#8881;padding:14px;border-radius:10px;overflow:auto;font-size:13px}
section{margin-bottom:22px}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;opacity:.6;margin:0 0 8px}
a{color:inherit}
</style></head><body>
<h1>Мок-шлюз SO-101</h1>
<p class="sub">Реализует docs/api.md без ROS и железа. Интерфейс:
<a href="http://localhost:5173/control">/control</a> ·
<a href="http://localhost:5173/">лендинг</a> ·
<a href="http://localhost:5173/admin">/admin</a> (токен <code>${ADMIN_TOKEN}</code>)</p>

<section><h2>Виртуальные посетители</h2>
<button onclick="go('/mock/bots/add')">+ бот в очередь</button>
<button onclick="go('/mock/bots/remove')">− бот</button>
<button onclick="go('/mock/bots/set?n=3')">сразу 3 бота</button>
</section>

<section><h2>Ход</h2>
<button onclick="go('/mock/kick')">снять текущего оператора</button>
</section>

<section><h2>Аварийная остановка</h2>
<button class="danger" onclick="go('/mock/estop')">СТОП</button>
<button onclick="go('/mock/release_estop')">снять стоп</button>
</section>

<section><h2>Поза руки</h2>
<button onclick="go('/mock/zero')">нулевая поза (вне рабочей зоны)</button>
</section>

<section><h2>Сброс</h2>
<button onclick="go('/mock/reset')">очистить всё</button>
</section>

<pre id="out">нажмите кнопку…</pre>
<script>
async function go(u){
  const r = await fetch(u,{method:'POST'});
  document.getElementById('out').textContent = JSON.stringify(await r.json(),null,2);
}
setInterval(()=>fetch('/mock/state',{method:'POST'})
  .then(r=>r.json())
  .then(j=>document.getElementById('out').textContent=JSON.stringify(j,null,2)),1000);
</script>
</body></html>`;

/* ------------------------------------------------------------------ запуск */

function short(token) {
  return String(token).slice(0, 8);
}

function log(msg) {
  const t = new Date().toISOString().slice(11, 19);
  console.log(`[${t}] ${msg}`);
}

if (START_ZERO_POSE) robotModel.snapTo(ZERO_POSE);

const initialBots = Number(process.env.MOCK_BOTS ?? 0);
for (let i = 0; i < initialBots; i++) addBot();

server.listen(PORT, () => {
  console.log(`\n  Мок-шлюз SO-101 (docs/api.md, protocol 1)`);
  console.log(`  HTTP/WS   http://localhost:${PORT}`);
  console.log(`  Панель    http://localhost:${PORT}/mock`);
  console.log(`  Видео     ${FEATURE_VIDEO ? `http://localhost:${PORT}/video/stream` : 'выключено'}`);
  console.log(`  Cartesian ${FEATURE_CARTESIAN ? 'включён' : 'выключен (вкладка «Точка» скрыта)'}`);
  console.log(`  Ход ${CONTROL_DURATION_SEC} с · бездействие ${IDLE_TIMEOUT_SEC} с · cooldown ${COOLDOWN_SEC} с`);
  console.log(`  Watchdog ${WATCHDOG_TIMEOUT_SEC} с (нужен ping ≥1 Гц) · лимит ${MAX_MSG_PER_SEC} сообщений/с`);
  console.log(`  Админка   http://localhost:5173/admin · токен ${ADMIN_TOKEN}`);
  console.log(`  Поза      ${START_ZERO_POSE ? 'нулевая (ВНЕ рабочей зоны)' : 'home'}\n`);
});
