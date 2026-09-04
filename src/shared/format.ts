/** Shared display formatters.
 *
 * The CNY formatter was redefined in eight modules, half of them passing
 * `minimumFractionDigits: 2` and half relying on the CNY default -- which
 * resolves to 2 either way, so the variants rendered identically. One
 * definition keeps it that way instead of leaving it to coincidence.
 *
 * Minor-unit conversion stays in `api.ts` next to the wire types; money must
 * not have two conversions.
 */

export const currency = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  minimumFractionDigits: 2,
})
