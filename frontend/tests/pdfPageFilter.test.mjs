import assert from 'node:assert/strict'
import test from 'node:test'
import { createCanvas } from 'canvas'
import { OPS } from 'pdfjs-dist'
import * as pageFilter from '../src/lib/pdfPageFilter.ts'

function canvasWithPhoto() {
  const canvas = createCanvas(80, 60)
  canvas.ownerDocument = { createElement: () => createCanvas(1, 1) }
  const ctx = canvas.getContext('2d')
  ctx.fillStyle = 'white'
  ctx.fillRect(0, 0, 80, 60)
  ctx.fillStyle = 'black'
  ctx.fillRect(2, 2, 4, 4)
  for (const [i, color] of ['#eeeeee', '#333333', '#888480', '#dc2814'].entries()) {
    ctx.fillStyle = color
    ctx.fillRect(20 + i * 10, 20, 10, 20)
  }
  return canvas
}

const pixels = (canvas, x, y, w = 1, h = 1) =>
  canvas.getContext('2d').getImageData(x, y, w, h).data

test('keeps all photo pixels while darkening paper and lightening text', async () => {
  const canvas = canvasWithPhoto()
  const photo = pixels(canvas, 20, 20, 40, 20)
  await pageFilter.applyPageFilterChunked(canvas, [[40, 0, 0, 20, 20, 20]], 'dark')
  assert.deepEqual(pixels(canvas, 20, 20, 40, 20), photo)
  assert.ok(pixels(canvas, 0, 0)[0] < 40)
  assert.ok(pixels(canvas, 3, 3)[0] > 200)
})

test('tracks nested form transforms and restores the parent transform', () => {
  const list = {
    fnArray: [OPS.save, OPS.transform, OPS.paintFormXObjectBegin,
      OPS.paintImageXObject, OPS.paintFormXObjectEnd, OPS.restore, OPS.paintInlineImageXObject],
    argsArray: [[], [2, 0, 0, 3, 10, 20], [[4, 0, 0, 5, 6, 7], null],
      ['photo'], [], [], [{}]],
  }
  assert.deepEqual(pageFilter.getImageTransforms(list, [2, 0, 0, -2, 0, 200]), [
    [16, 0, 0, -30, 44, 118],
    [2, 0, 0, -2, 0, 200],
  ])
})

test('protects repeated and grouped images using their individual placements', () => {
  const list = {
    fnArray: [OPS.paintImageXObjectRepeat, OPS.paintInlineImageXObjectGroup],
    argsArray: [['photo', 10, 20, [5, 6, 30, 40]], [{}, [
      { transform: [0, 10, -20, 0, 50, 60] },
    ]]],
  }
  assert.deepEqual(pageFilter.getImageTransforms(list, [1, 0, 0, 1, 0, 0]), [
    [10, 0, 0, 20, 5, 6], [10, 0, 0, 20, 30, 40], [0, 10, -20, 0, 50, 60],
  ])
})

test('protects rotated images without protecting the whole bounding rectangle', async () => {
  const canvas = canvasWithPhoto()
  const original = pixels(canvas, 30, 30)
  await pageFilter.applyPageFilterChunked(canvas, [[20, 20, -20, 20, 30, 10]], 'dark')
  assert.deepEqual(pixels(canvas, 30, 30), original)
  assert.ok(pixels(canvas, 11, 11)[0] < 40)
})

test('keeps thin colored text strokes and chart colors', async () => {
  const canvas = createCanvas(60, 40)
  canvas.ownerDocument = { createElement: () => createCanvas(1, 1) }
  const ctx = canvas.getContext('2d')
  ctx.fillStyle = 'white'
  ctx.fillRect(0, 0, 60, 40)
  ctx.fillStyle = 'rgb(220, 30, 30)'
  ctx.fillRect(4, 5, 4, 30) // narrow vector stroke
  ctx.fillRect(24, 10, 12, 20) // solid chart region
  await pageFilter.applyPageFilterChunked(canvas, [], 'dark')
  for (const x of [5, 29]) {
    const [r, g, b] = pixels(canvas, x, 20)
    assert.ok(r > 180 && g < 50 && b < 50, `${x}: ${[r, g, b]}`)
  }
})

test('keeps antialiased blue link text blue instead of adding a gray halo', async () => {
  const canvas = createCanvas(350, 100)
  canvas.ownerDocument = { createElement: () => createCanvas(1, 1) }
  const ctx = canvas.getContext('2d')
  ctx.fillStyle = 'white'
  ctx.fillRect(0, 0, 350, 100)
  ctx.font = '32px serif'
  ctx.fillStyle = '#0000ee'
  ctx.fillText('Giorgi and Bader', 5, 52)
  const original = pixels(canvas, 0, 0, 350, 100).slice()

  await pageFilter.applyPageFilterChunked(canvas, [], 'dark')
  const filtered = pixels(canvas, 0, 0, 350, 100)
  let bluePixels = 0
  let preserved = 0
  let edgePixels = 0
  let brightEdges = 0
  for (let i = 0; i < original.length; i += 4) {
    if (original[i + 2] - original[i] <= 30) continue
    bluePixels += 1
    if (filtered[i + 2] - filtered[i] > 30) preserved += 1
    if (original[i + 2] - original[i] > 48 && original[i] > 100) {
      edgePixels += 1
      if (filtered[i] > 70) brightEdges += 1
    }
  }
  assert.ok(bluePixels > 1000)
  assert.ok(preserved > bluePixels * 0.85, `${preserved}/${bluePixels} blue pixels survived`)
  assert.ok(edgePixels > 100)
  assert.ok(brightEdges < edgePixels * 0.05, `${brightEdges}/${edgePixels} edges stay bright`)
})

test('does not expose partly filtered page bands between animation frames', async () => {
  const canvas = createCanvas(1200, 1800)
  canvas.ownerDocument = { createElement: () => createCanvas(1, 1) }
  const ctx = canvas.getContext('2d')
  ctx.fillStyle = 'white'
  ctx.fillRect(0, 0, 1200, 1800)
  const frames = []
  const originalRaf = globalThis.requestAnimationFrame
  globalThis.requestAnimationFrame = (callback) => {
    frames.push([pixels(canvas, 0, 0)[0], pixels(canvas, 0, 1799)[0]])
    return setImmediate(callback)
  }
  try {
    await pageFilter.applyPageFilterChunked(canvas, [], 'dark')
  } finally {
    globalThis.requestAnimationFrame = originalRaf
  }
  assert.ok(frames.length > 1)
  assert.ok(frames.every(([top, bottom]) => top === 255 && bottom === 255), JSON.stringify(frames))
  assert.ok(pixels(canvas, 0, 0)[0] < 40)
  assert.ok(pixels(canvas, 0, 1799)[0] < 40)
})
