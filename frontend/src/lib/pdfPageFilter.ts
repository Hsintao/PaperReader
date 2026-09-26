import { OPS, Util } from 'pdfjs-dist'
import type { PDFPageProxy } from 'pdfjs-dist'

type PDFOperatorList = Awaited<ReturnType<PDFPageProxy['getOperatorList']>>

// Reading tones that repaint a rendered page. 'light' leaves the page as the
// document drew it, so it needs no filter.
export type PdfPageTone = 'gray' | 'dark'

export function pageToneForTheme(theme: string | undefined): PdfPageTone | null {
  return theme === 'gray' || theme === 'dark' ? theme : null
}

// Image placement matrices map the image unit square into canvas pixels.
export function getImageTransforms(operators: PDFOperatorList, viewportTransform: number[]): number[][] {
  let transform = viewportTransform
  const stack: number[][] = []
  const images: number[][] = []
  for (let i = 0; i < operators.fnArray.length; i += 1) {
    const args = operators.argsArray[i]
    switch (operators.fnArray[i]) {
      case OPS.save:
      case OPS.beginGroup:
        stack.push(transform)
        break
      case OPS.restore:
      case OPS.paintFormXObjectEnd:
      case OPS.endGroup:
        transform = stack.pop() || transform
        break
      case OPS.transform:
        transform = Util.transform(transform, args)
        break
      case OPS.paintFormXObjectBegin:
        stack.push(transform)
        if (args[0]) transform = Util.transform(transform, args[0])
        break
      case OPS.paintImageXObject:
      case OPS.paintInlineImageXObject:
        images.push(transform)
        break
      case OPS.paintImageXObjectRepeat:
        for (let j = 0; j < args[3].length; j += 2) {
          images.push(Util.transform(transform, [args[1], 0, 0, args[2], args[3][j], args[3][j + 1]]))
        }
        break
      case OPS.paintInlineImageXObjectGroup:
        for (const entry of args[1]) images.push(Util.transform(transform, entry.transform))
        break
    }
  }
  return images
}

// Keep embedded images intact, including their grayscale pixels. Outside images,
// map neutral paper and text through an affine curve c' = offset + c * scale and
// retain saturated vector colors (charts, diagrams, link boxes).
//
// Exception: LCD subpixel antialiasing (the opaque canvas react-pdf renders
// into enables it) paints glyph edges as saturated red/blue fringes, which are
// per-channel blends of neutral paper and ink. Preserving them like chart
// colors keeps the fringes bright, so inverted pages show ghosted text. A
// saturated pixel that touches a neutral one is such a fringe: desaturate it
// to its luma and map it like any other paper/ink blend. Only saturated pixels
// surrounded by saturated ones (solid chart regions) keep their color.
const SAT_FULL = 12
const SAT_NONE = 48

const TONES: Record<PdfPageTone, { offset: number; scale: number }> = {
  // Ink (0) becomes light and paper (255) becomes near-black, so the page reads
  // as a dark surface with light text.
  dark: { offset: 217.65, scale: -0.743 },
  // Paper (255) drops to ~#d7d7d7 and ink (0) stays dark at ~#242424: the page
  // is dimmed without inverting, which keeps figures legible.
  gray: { offset: 36, scale: 0.7 },
}

function saturationAt(data: Uint8ClampedArray, i: number): number {
  const r = data[i]
  const g = data[i + 1]
  const b = data[i + 2]
  const max = r > g ? (r > b ? r : b) : g > b ? g : b
  const min = r < g ? (r < b ? r : b) : g < b ? g : b
  return max - min
}

// `data` holds `width`-pixel rows; only rows [coreTop, coreTop + coreHeight)
// are written, but the rows may include a 1px halo on each side so fringe
// detection can read unmodified neighbors across band boundaries.
function mapNeutralPixels(
  data: Uint8ClampedArray,
  tone: { offset: number; scale: number },
  protectedPixels: Uint8ClampedArray | undefined,
  width: number,
  coreTop: number,
  coreHeight: number
): void {
  const { offset, scale } = tone
  const shift = scale - 1
  const rows = data.length / 4 / width
  // Neutrality must be judged from the original pixels, before any in-place
  // writes below, so fringe detection never reads an already-mapped neighbor.
  const neutral = new Uint8Array(width * rows)
  for (let p = 0, i = 0; i < data.length; i += 4, p += 1) {
    neutral[p] = saturationAt(data, i) <= SAT_FULL ? 1 : 0
  }
  for (let row = coreTop; row < coreTop + coreHeight; row += 1) {
    for (let col = 0; col < width; col += 1) {
      const p = row * width + col
      const i = p * 4
      if (protectedPixels?.[i + 3]) continue
      const r = data[i]
      const g = data[i + 1]
      const b = data[i + 2]
      const sat = saturationAt(data, i)
      if (sat <= SAT_FULL) {
        data[i] = offset + scale * r
        data[i + 1] = offset + scale * g
        data[i + 2] = offset + scale * b
        continue
      }
      const touchesNeutral =
        (col > 0 && neutral[p - 1]) ||
        (col < width - 1 && neutral[p + 1]) ||
        (row > 0 && neutral[p - width]) ||
        (row < rows - 1 && neutral[p + width]) ||
        (col > 0 && row > 0 && neutral[p - width - 1]) ||
        (col < width - 1 && row > 0 && neutral[p - width + 1]) ||
        (col > 0 && row < rows - 1 && neutral[p + width - 1]) ||
        (col < width - 1 && row < rows - 1 && neutral[p + width + 1])
      if (touchesNeutral) {
        const luma = 0.299 * r + 0.587 * g + 0.114 * b
        data[i] = data[i + 1] = data[i + 2] = offset + scale * luma
        continue
      }
      if (sat >= SAT_NONE) continue
      const t = (SAT_NONE - sat) / (SAT_NONE - SAT_FULL)
      data[i] = r + t * (offset + shift * r)
      data[i + 1] = g + t * (offset + shift * g)
      data[i + 2] = b + t * (offset + shift * b)
    }
  }
}

function buildMaskContext(canvas: HTMLCanvasElement, imageTransforms: number[][]): CanvasRenderingContext2D | undefined {
  if (!imageTransforms.length) return undefined
  const mask = canvas.ownerDocument.createElement('canvas')
  mask.width = canvas.width
  mask.height = canvas.height
  const maskCtx = mask.getContext('2d')!
  for (const [a, b, c, d, e, f] of imageTransforms) {
    maskCtx.setTransform(a, b, c, d, e, f)
    maskCtx.fillRect(0, 0, 1, 1)
  }
  return maskCtx
}

const nextFrame = () => new Promise<void>((resolve) => {
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => resolve())
  else setTimeout(resolve, 0)
})

// Chunked variant: process the canvas in horizontal bands and yield a frame
// between bands, so filtering several pages in the render window cannot block
// scrolling. Aborts early when the canvas was re-rendered in the meantime
// (zoom/theme change) or isStale() reports the page element was replaced.
export async function applyPageFilterChunked(
  canvas: HTMLCanvasElement,
  imageTransforms: number[][],
  tone: PdfPageTone,
  isStale: () => boolean = () => false
): Promise<void> {
  const ctx = canvas.getContext('2d')
  if (!ctx || canvas.width === 0 || canvas.height === 0) return
  const { width, height } = canvas
  const toneMap = TONES[tone]
  const maskCtx = buildMaskContext(canvas, imageTransforms)
  const bandHeight = Math.max(256, Math.floor(1_000_000 / width))
  for (let y = 0; y < height; y += bandHeight) {
    if (isStale() || canvas.width !== width || canvas.height !== height) return
    const h = Math.min(bandHeight, height - y)
    // Read a 1px halo around the band so fringe detection sees the original
    // neighbors across band boundaries; only the core rows are written back.
    const haloTop = y > 0 ? 1 : 0
    const haloBottom = y + h < height ? 1 : 0
    const y0 = y - haloTop
    const bandWithHalo = h + haloTop + haloBottom
    const protectedPixels = maskCtx?.getImageData(0, y0, width, bandWithHalo).data
    const image = ctx.getImageData(0, y0, width, bandWithHalo)
    mapNeutralPixels(image.data, toneMap, protectedPixels, width, haloTop, h)
    ctx.putImageData(image, 0, y0, 0, haloTop, width, h)
    await nextFrame()
  }
}
