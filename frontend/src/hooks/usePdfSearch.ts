import { useCallback, useEffect, useRef, useState } from 'react'
import { collectMatches } from '../lib/pdfSearch'
import { isCurrentRequest, nextRequestId } from '../lib/requestGuard'
import { normalized, type Match } from '../lib/pdfText'

type Options = {
  active?: boolean
  getPageText: (page: number) => Promise<string>
  numPages: number
  goto: (page: number) => void
  onRepaint: () => void
}

// Whole-document find (Ctrl+F).  Matches are computed over the cached page
// texts and painted by the pane's overlay pass; navigation jumps to the page
// holding the current match.
export function usePdfSearch({ active = true, getPageText, numPages, goto, onRepaint }: Options) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [matches, setMatches] = useState<Match[]>([])
  const [current, setCurrent] = useState(0)
  const [searching, setSearching] = useState(false)
  const stateRef = useRef({ open, query, matches, current })
  stateRef.current = { open, query, matches, current }
  // Every scan takes a fresh id; only the newest one may publish results, so a
  // slow query can never overwrite the matches of the one typed after it.
  const requestIdRef = useRef(0)

  // A document change (page count reload) invalidates whatever is in flight.
  useEffect(() => {
    requestIdRef.current = nextRequestId(requestIdRef.current)
    setSearching(false)
  }, [numPages, getPageText])

  useEffect(() => () => {
    requestIdRef.current = nextRequestId(requestIdRef.current)
  }, [])

  const run = useCallback(
    async (rawQuery: string) => {
      const needle = normalized(rawQuery)
      const requestId = nextRequestId(requestIdRef.current)
      requestIdRef.current = requestId
      if (!needle || !numPages) {
        setMatches([])
        setCurrent(0)
        setSearching(false)
        onRepaint()
        return
      }
      setSearching(true)
      const found = await collectMatches(needle, numPages, getPageText, () =>
        isCurrentRequest(requestId, requestIdRef.current)
      )
      // A newer run (or an invalidation) owns the search state now.
      if (!found) return
      setMatches(found)
      setCurrent(0)
      setSearching(false)
      onRepaint()
      if (found.length) goto(found[0].page)
    },
    [getPageText, goto, numPages, onRepaint]
  )

  const openSearch = useCallback(() => {
    setOpen(true)
  }, [])

  const closeSearch = useCallback(() => {
    requestIdRef.current = nextRequestId(requestIdRef.current)
    setOpen(false)
    setQuery('')
    setMatches([])
    setCurrent(0)
    setSearching(false)
    onRepaint()
  }, [onRepaint])

  const step = useCallback(
    (delta: number) => {
      const { matches: list, current: index } = stateRef.current
      if (!list.length) return
      const nextIndex = (index + delta + list.length) % list.length
      setCurrent(nextIndex)
      goto(list[nextIndex].page)
      onRepaint()
    },
    [goto, onRepaint]
  )

  const updateQuery = useCallback(
    (value: string) => {
      setQuery(value)
      if (!value.trim()) {
        // Clearing the query drops any in-flight scan with it.
        requestIdRef.current = nextRequestId(requestIdRef.current)
        setMatches([])
        setCurrent(0)
        setSearching(false)
        onRepaint()
        return
      }
      void run(value)
    },
    [onRepaint, run]
  )

  // Escape closes; the browser find is suppressed by the ReaderPage-level
  // Ctrl/Cmd+F handler, so no extra interception here.
  useEffect(() => {
    if (!active || !open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        closeSearch()
      } else if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault()
        step(1)
      } else if (event.key === 'Enter' && event.shiftKey) {
        event.preventDefault()
        step(-1)
      }
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [active, closeSearch, open, step])

  return {
    open,
    query,
    matches,
    current,
    searching,
    stateRef,
    openSearch,
    closeSearch,
    updateQuery,
    next: () => step(1),
    prev: () => step(-1),
  }
}
