// Guards async work against state that moved on while it was in flight.

// True when an async result still belongs to the document it was requested for.
// Handlers capture the document id they started with and drop the result once
// the reader has switched to another document.
export function isCurrentDocument(requestedId: string, currentId: string | undefined): boolean {
  return requestedId === currentId
}

// Monotonic sequence numbers for requests that supersede their predecessors
// (find-in-document): every run takes a fresh id, and a response may only
// publish while its id is still the newest.
export function nextRequestId(current: number): number {
  return current + 1
}

export function isCurrentRequest(requestId: number, current: number): boolean {
  return requestId === current
}
