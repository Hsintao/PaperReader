import { OPS, Util } from 'pdfjs-dist'
import type { PDFPageProxy } from 'pdfjs-dist'

type PDFOperatorList = Awaited<ReturnType<PDFPageProxy['getOperatorList']>>

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
// map neutral paper/text to dark-mode tones and retain saturated vector colors.
const SAT_FULL = 12
const SAT_NONE = 48

export function applyDarkPageFilter(canvas: HTMLCanvasElement, imageTransforms: number[][]): void {
  const ctx = canvas.getContext('2d')
  if (!ctx || canvas.width === 0 || canvas.height === 0) return
  let protectedPixels: Uint8ClampedArray | undefined
  if (imageTransforms.length) {
    const mask = canvas.ownerDocument.createElement('canvas')
    mask.width = canvas.width
    mask.height = canvas.height
    const maskCtx = mask.getContext('2d')!
    for (const [a, b, c, d, e, f] of imageTransforms) {
      maskCtx.setTransform(a, b, c, d, e, f)
      maskCtx.fillRect(0, 0, 1, 1)
    }
    protectedPixels = maskCtx.getImageData(0, 0, mask.width, mask.height).data
  }
  const image = ctx.getImageData(0, 0, canvas.width, canvas.height)
  const data = image.data
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
    data[i] = r + t * (217.65 - 1.743 * r)
    data[i + 1] = g + t * (217.65 - 1.743 * g)
    data[i + 2] = b + t * (217.65 - 1.743 * b)
  }
  ctx.putImageData(image, 0, 0)
}
