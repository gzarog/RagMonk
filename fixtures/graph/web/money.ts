export function normalizeTotal(total: number): number {
  return Math.round(total * 100) / 100;
}
