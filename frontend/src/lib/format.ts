/** Текстовые мелочи, от которых зависит, звучит ли интерфейс по-человечески. */

/** «2 мин» вместо «138 с». Точность прохожему не нужна, нужна оценка. */
export function formatWait(seconds: number): string {
  if (seconds <= 0) return 'вот-вот';
  if (seconds < 60) return `${Math.ceil(seconds / 5) * 5} секунд`;
  const minutes = Math.round(seconds / 60);
  if (minutes <= 1) return 'около минуты';
  if (minutes < 5) return `около ${minutes} минут`;
  return `больше ${minutes} минут`;
}

/** Русское склонение по числу: plural(3, 'человек', 'человека', 'человек'). */
export function plural(n: number, one: string, few: string, many: string): string {
  const abs = Math.abs(n);
  const mod10 = abs % 10;
  const mod100 = abs % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

/** «впереди 3 человека» */
export function peopleAhead(n: number): string {
  return `${n} ${plural(n, 'человек', 'человека', 'человек')}`;
}
