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

function mapNeutralPixels(
  data: Uint8ClampedArray,
  tone: { offset: number; scale: number },
  protectedPixels?: Uint8ClampedArray
): void {
  const { offset, scale } = tone
  const shift = scale - 1
  for (let i = 0; i < data.length; i += 4) {
    if (protectedPixels?.[i + 3]) continue
    const r = data[i]
    const g = data[i + 1]
    const b = data[i + 2]
    const max = r > g ? (r > b ? r : b) : g > b ? g : b
    const min = r < g ? (r < b ? r : b) : g < b ? g : b
    const sat = max - min
    if (sat >= SAT_NONE) continue
    const t = sat <= SAT_FULL ? 1 : (SAT_NONE - sat) / (SAT_NONE - SAT_FULL)
    data[i] = r + t * (offset + shift * r)
    data[i + 1] = g + t * (offset + shift * g)
    data[i + 2] = b + t * (offset + shift * b)
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
    const protectedPixels = maskCtx?.getImageData(0, y, width, h).data
    const image = ctx.getImageData(0, y, width, h)
    mapNeutralPixels(image.data, toneMap, protectedPixels)
    ctx.putImageData(image, 0, y)
    await nextFrame()
  }
}
