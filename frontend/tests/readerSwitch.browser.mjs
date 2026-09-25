// Prepend const TEST_SPACE_ID = <ego task space>; and run via ego-browser nodejs.
// The task's p1 must show a local dev reader with at least two completed papers.
const assert = (await import('node:assert/strict')).default
const task = await taskSpace(TEST_SPACE_ID)
const page = task.page('p1')
await page.waitForSelector('.react-pdf__Page__textContent span')
const result = await page.evaluate(async () => {
  const visiblePane = () => [...document.querySelectorAll('.pdf-pane')].find(el => el.getClientRects().length)
  const wait = (predicate) => new Promise((resolve, reject) => {
    const deadline = performance.now() + 20000
    const poll = () => predicate() ? resolve() : performance.now() > deadline ? reject(new Error('PDF did not become ready')) : requestAnimationFrame(poll)
    poll()
  })
  const items = [...document.querySelectorAll('.doc-item')]
  const first = items.find(el => el.classList.contains('active'))
  const second = items.find(el => el !== first && el.querySelector('.doc-name')?.textContent !== first.querySelector('.doc-name')?.textContent && el.textContent.includes('done'))
  if (!first || !second) throw new Error('Two completed papers are required')
  visiblePane().querySelector('[title="放大"]').click()
  await wait(() => visiblePane().querySelector('[aria-label="缩放百分比"]').value === '110')
  await wait(() => visiblePane().querySelector('.react-pdf__Page__textContent span') && !visiblePane().querySelector('.react-pdf__message--loading'))
  const original = visiblePane().querySelector('canvas')
  const measurements = []
  for (const item of [second, first]) {
    const oldTitle = visiblePane().querySelector('.pdf-title').textContent
    const start = performance.now()
    item.click()
    await wait(() => visiblePane()?.querySelector('.pdf-title')?.textContent !== oldTitle && visiblePane()?.querySelector('.react-pdf__Page__textContent span') && !visiblePane()?.querySelector('.react-pdf__message--loading'))
    measurements.push(Math.round(performance.now() - start))
  }
  return { measurements, sameCanvas: visiblePane().querySelector('canvas') === original, zoom: visiblePane().querySelector('[aria-label="缩放百分比"]').value }
})
console.log(result)
assert.equal(result.sameCanvas, true, 'A → B → A must reuse the rendered canvas')
assert.equal(result.zoom, '110', 'Returning to a cached paper must preserve zoom')
