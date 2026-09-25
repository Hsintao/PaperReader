import assert from 'node:assert/strict'
import test from 'node:test'
import { createCanvas } from 'canvas'
import { OPS } from 'pdfjs-dist'
import * as darkMode from '../src/lib/pdfDarkMode.ts'

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

test('keeps all photo pixels while darkening paper and lightening text', () => {
  const canvas = canvasWithPhoto()
  const photo = pixels(canvas, 20, 20, 40, 20)
  darkMode.applyDarkPageFilter(canvas, [[40, 0, 0, 20, 20, 20]])
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
  assert.deepEqual(darkMode.getImageTransforms(list, [2, 0, 0, -2, 0, 200]), [
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
  assert.deepEqual(darkMode.getImageTransforms(list, [1, 0, 0, 1, 0, 0]), [
    [10, 0, 0, 20, 5, 6], [10, 0, 0, 20, 30, 40], [0, 10, -20, 0, 50, 60],
  ])
})

test('protects rotated images without protecting the whole bounding rectangle', () => {
  const canvas = canvasWithPhoto()
  const original = pixels(canvas, 30, 30)
  darkMode.applyDarkPageFilter(canvas, [[20, 20, -20, 20, 30, 10]])
  assert.deepEqual(pixels(canvas, 30, 30), original)
  assert.ok(pixels(canvas, 11, 11)[0] < 40)
})
