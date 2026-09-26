import assert from 'node:assert/strict'
import test from 'node:test'
import { isCurrentDocument, isCurrentRequest, nextRequestId } from '../src/lib/requestGuard.ts'
import { collectMatches, MAX_MATCHES } from '../src/lib/pdfSearch.ts'

// Mirrors usePdfSearch.run: every scan takes the newest request id and may only
// publish while it still owns it.
function startScan(idRef, query, numPages, getPageText) {
  const requestId = nextRequestId(idRef.current)
  idRef.current = requestId
  return collectMatches(query, numPages, getPageText, () =>
    isCurrentRequest(requestId, idRef.current)
  )
}

test('a stale search cannot replace the newer results', async () => {
  const idRef = { current: 0 }
  const slow = () => new Promise((resolve) => setTimeout(() => resolve('alphaalpha'), 20))
  const fast = () => Promise.resolve('beta')
  const stale = startScan(idRef, 'alpha', 2, slow)
  const newest = startScan(idRef, 'beta', 2, fast)
  assert.equal(await stale, null)
  assert.deepEqual(await newest, [
    { page: 1, start: 0, length: 4 },
    { page: 2, start: 0, length: 4 },
  ])
})

test('an empty query invalidates an in-flight search', async () => {
  const idRef = { current: 0 }
  const slow = () => new Promise((resolve) => setTimeout(() => resolve('alpha'), 10))
  const inFlight = startScan(idRef, 'alpha', 1, slow)
  // updateQuery('') takes a fresh id without starting a scan.
  idRef.current = nextRequestId(idRef.current)
  assert.equal(await inFlight, null)
  assert.equal(await startScan(idRef, '', 1, () => Promise.resolve('alpha')), null)
})

test('a result for a document that is no longer active is ignored', async () => {
  assert.equal(isCurrentDocument('doc-a', 'doc-a'), true)
  assert.equal(isCurrentDocument('doc-a', 'doc-b'), false)
  assert.equal(isCurrentDocument('doc-a', undefined), false)

  // Mirrors an annotation handler awaiting its request while the reader
  // switches documents.
  let activeId = 'doc-a'
  const publish = async () => {
    const requestedId = activeId
    await new Promise((resolve) => setTimeout(resolve, 5))
    return isCurrentDocument(requestedId, activeId) ? `list:${requestedId}` : null
  }
  const inFlight = publish()
  activeId = 'doc-b'
  assert.equal(await inFlight, null)
  assert.equal(await publish(), 'list:doc-b')
})

test('collects matches up to the cap and stops once the request is stale', async () => {
  const text = 'a'.repeat(MAX_MATCHES + 10)
  const found = await collectMatches('a', 1, () => Promise.resolve(text), () => true)
  assert.equal(found.length, MAX_MATCHES)

  let checks = 0
  const dropped = await collectMatches(
    'alpha',
    3,
    () => Promise.resolve('alpha'),
    () => (checks += 1) < 2
  )
  assert.equal(dropped, null)
})

test('page texts are fetched with overlapping requests', async () => {
  // One await per page serialized whole-document search on the worker round
  // trip; batched fetches must overlap while keeping results in page order.
  let inFlight = 0
  let peak = 0
  const getPageText = (page) => {
    inFlight += 1
    peak = Math.max(peak, inFlight)
    return new Promise((resolve) =>
      setTimeout(() => {
        inFlight -= 1
        resolve(page % 2 ? 'alpha' : '')
      }, 5)
    )
  }
  const found = await collectMatches('alpha', 12, getPageText, () => true)
  assert.equal(peak > 1, true)
  assert.deepEqual(found.map((m) => m.page), [1, 3, 5, 7, 9, 11])
})
