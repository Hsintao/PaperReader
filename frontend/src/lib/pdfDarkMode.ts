// Dark-mode rendering for PDF page canvases.
//
// A rendered page is a single bitmap, so a blanket CSS invert turns photos
// and colored figures into negatives. Instead, invert only near-grayscale
// pixels (paper background and text) and keep saturated pixels — figure
// content — in their original colors. Grayscale pixels are mapped to
// 217.65 - 0.743 * v, matching invert(0.93) brightness(0.96) contrast(0.9),
// with a soft blend across the saturation threshold to avoid hard edges on
// anti-aliased pixels.

const SAT_FULL = 12
const SAT_NONE = 48

export function applyDarkPageFilter(canvas: HTMLCanvasElement): void {
  const ctx = canvas.getContext('2d')
  if (!ctx || canvas.width === 0 || canvas.height === 0) return
  const image = ctx.getImageData(0, 0, canvas.width, canvas.height)
  const data = image.data
  for (let i = 0; i < data.length; i += 4) {
    const r = data[i]
    const g = data[i + 1]
    const b = data[i + 2]
    const max = r > g ? (r > b ? r : b) : g > b ? g : b
    const min = r < g ? (r < b ? r : b) : g < b ? g : b
    const sat = max - min
    if (sat >= SAT_NONE) continue
    const t = sat <= SAT_FULL ? 1 : (SAT_NONE - sat) / (SAT_NONE - SAT_FULL)
    data[i] = r + t * (217.65 - 1.743 * r)
    data[i + 1] = g + t * (217.65 - 1.743 * g)
    data[i + 2] = b + t * (217.65 - 1.743 * b)
  }
  ctx.putImageData(image, 0, 0)
}
