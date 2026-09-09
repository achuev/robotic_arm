import type { ErrorCode, ErrorMsg, RevokeReason } from './protocol';
import { axisEdgeHint } from './workspace';

/**
 * Как UI отвечает на каждый код ошибки из docs/api.md §«Коды ошибок».
 *
 * `severity`:
 *   silent  — ничего не показываем (человек не виноват и ничего сделать не может);
 *   toast   — всплывающая подсказка, сама уходит;
 *   banner  — постоянная плашка, пока причина не исчезнет.
 */
export type ErrorSeverity = 'silent' | 'toast' | 'banner';

export interface ErrorPresentation {
  code: ErrorCode;
  /** Заголовок для человека. Пустой — если ничего не показываем. */
  title: string;
  /** Пояснение под заголовком. */
  hint: string;
  severity: ErrorSeverity;
  /** Сколько миллисекунд держать тост. */
  ttlMs: number;
  /** Побочные эффекты, которые обязан выполнить транспорт/автомат. */
  effect: ErrorEffect;
  /**
   * Наш текст важнее серверного `message`.
   *
   * По умолчанию мы предпочитаем формулировку сервера: она конкретнее. Но
   * есть случаи, где серверная строка — диагностика («Точка вне рабочей
   * зоны»), а человеку нужно объяснение, а не диагноз.
   */
  preferOwnCopy?: boolean;
}

export type ErrorEffect =
  /** Ничего, кроме показа текста. */
  | 'none'
  /** Мы считали себя оператором, но это не так — вернуться в наблюдатели. */
  | 'demote_to_observer'
  /** Нас нет в очереди — показать кнопку «встать в очередь». */
  | 'offer_enqueue'
  /** Притормозить исходящий поток. */
  | 'backoff'
  /** Подсветить орган, названный сервером в `joint`/`axis`. */
  | 'highlight_joint'
  /** Поднять флаг аварийной остановки. */
  | 'raise_estop'
  /** Переподключиться. */
  | 'reconnect'
  /** Запретить постановку в очередь до конца cooldown. */
  | 'start_cooldown';

const TABLE: Record<ErrorCode, Omit<ErrorPresentation, 'code'>> = {
  not_controller: {
    title: 'Роботом управляет кто-то другой',
    hint: 'Встаньте в очередь — ход перейдёт к вам автоматически.',
    severity: 'toast',
    ttlMs: 4000,
    effect: 'demote_to_observer',
  },
  not_queued: {
    // Приходит на `leave` от того, кто в очереди не стоит.
    title: 'Вы пока просто смотрите',
    hint: 'Нажмите «Встать в очередь», чтобы получить ход.',
    severity: 'toast',
    ttlMs: 4000,
    effect: 'offer_enqueue',
  },
  queue_full: {
    title: 'Слишком много желающих',
    hint: 'Очередь заполнена. Зайдите чуть позже — она быстро двигается.',
    severity: 'toast',
    ttlMs: 6000,
    effect: 'none',
  },
  cooldown: {
    title: 'Дайте другим попробовать',
    hint: 'Вы только что управляли. Ещё немного — и можно снова.',
    severity: 'toast',
    ttlMs: 5000,
    effect: 'start_cooldown',
  },
  rate_limited: {
    // Контракт: «ничего, молча притормозить».
    title: '',
    hint: '',
    severity: 'silent',
    ttlMs: 0,
    effect: 'backoff',
  },
  out_of_range: {
    title: 'Дальше рука не гнётся',
    hint: 'Значение поставлено на предел сустава.',
    severity: 'toast',
    ttlMs: 2500,
    effect: 'highlight_joint',
  },
  ik_failed: {
    title: 'Сюда рука не дотянется',
    hint: 'Попробуйте точку поближе к основанию.',
    severity: 'toast',
    ttlMs: 3500,
    effect: 'none',
  },
  estop: {
    title: 'Робот остановлен оператором',
    hint: 'Аварийная остановка. Управление вернётся, когда её снимут.',
    severity: 'banner',
    ttlMs: 0,
    effect: 'raise_estop',
  },
  bad_message: {
    // Контракт: «ничего, переподключиться».
    title: '',
    hint: '',
    severity: 'silent',
    ttlMs: 0,
    effect: 'reconnect',
  },
};

/**
 * Никогда не бросает: неизвестный код (бэкенд ушёл вперёд) превращается
 * в нейтральный тост, а не в белый экран.
 */
export function describeError(code: string): ErrorPresentation {
  const known = TABLE[code as ErrorCode];
  if (known) return { code: code as ErrorCode, ...known };
  return {
    code: code as ErrorCode,
    title: 'Что-то пошло не так',
    hint: 'Попробуйте ещё раз.',
    severity: 'toast',
    ttlMs: 4000,
    effect: 'none',
  };
}

/**
 * Подача ошибки с учётом её ПОЛЕЙ, а не только кода.
 *
 * Единственный случай, где это важно, — `out_of_range`. Он приходит и когда
 * сустав упёрся в свой предел (`joint`), и когда схват дошёл до границы
 * рабочей зоны (`axis`). Второе — не поломка и даже не отказ: контракт §3
 * говорит, что значение клэмпится, а движение исполняется. На свежем стенде
 * рука включается ВНЕ зоны (x = 0.391 при границе 0.38), и первый же джог
 * законно приносит `out_of_range`. Если показать это словами «дальше рука не
 * гнётся», человек решит, что сломал робота на первом же касании.
 *
 * Поле `axis` берётся у сервера как есть: он называет ось, которая
 * действительно упёрлась, — она не обязана совпадать с той, по которой
 * джогали (диагональный шаг упирается в другую границу).
 */
export function describeErrorMessage(msg: ErrorMsg): ErrorPresentation {
  const base = describeError(msg.code);
  if (msg.code !== 'out_of_range' || !msg.axis) return base;
  return {
    ...base,
    title: 'Рука у края рабочей зоны',
    hint: axisEdgeHint(msg.axis),
    // Серверное «Точка вне рабочей зоны» — диагностика, а не объяснение.
    preferOwnCopy: true,
  };
}

/**
 * Сколько ещё ждать до следующей попытки встать в очередь.
 *
 * Контракт даёт `retry_after_sec` в теле ошибки `cooldown` — это источник
 * истины. Разбор текста остался только как страховка на случай, если шлюз
 * поле не положил: угадывать секунды по строке — плохая идея, но соврать
 * человеку про «ещё 30 с» вместо «ещё 3 с» ещё хуже.
 */
export function parseCooldownSeconds(
  msg: ErrorMsg | string,
  fallback: number
): number {
  if (typeof msg !== 'string' && typeof msg.retry_after_sec === 'number') {
    const n = msg.retry_after_sec;
    if (Number.isFinite(n) && n >= 0) return Math.ceil(n);
  }
  const text = typeof msg === 'string' ? msg : (msg.message ?? '');
  const m = /(\d+(?:[.,]\d+)?)/.exec(text);
  if (!m) return fallback;
  const n = Number(m[1].replace(',', '.'));
  return Number.isFinite(n) && n > 0 ? Math.ceil(n) : fallback;
}

/* ------------------------------------------------ причины отзыва хода */

export interface RevokeCopy {
  title: string;
  hint: string;
  /** Начинается ли после этого cooldown на постановку в очередь. */
  cooldown: boolean;
}

const REVOKE_TABLE: Record<RevokeReason, RevokeCopy> = {
  timeout: {
    title: 'Время вышло',
    hint: 'Ваши 90 секунд закончились — ход ушёл следующему.',
    cooldown: true,
  },
  idle: {
    title: 'Ход передан дальше',
    hint: 'Вы ничего не трогали, и робота отдали следующему в очереди.',
    cooldown: true,
  },
  release: {
    title: 'Вы отдали управление',
    hint: 'Спасибо! Робот достался следующему.',
    cooldown: true,
  },
  admin: {
    title: 'Управление забрал оператор стенда',
    hint: 'Так бывает — например, чтобы поправить руку.',
    cooldown: false,
  },
  estop: {
    title: 'Аварийная остановка',
    hint: 'Робота остановили. Управление вернётся, когда его перезапустят.',
    cooldown: false,
  },
  disconnect: {
    title: 'Связь пропала',
    hint: 'Телефон потерял сеть больше чем на 10 секунд, и ход ушёл дальше.',
    cooldown: false,
  },
};

export function describeRevoke(reason: string): RevokeCopy {
  return (
    REVOKE_TABLE[reason as RevokeReason] ?? {
      title: 'Ход завершён',
      hint: 'Можно встать в очередь снова.',
      cooldown: true,
    }
  );
}
