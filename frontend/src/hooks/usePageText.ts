import { useCallback, useEffect, useRef } from 'react'
import { normalized } from '../lib/pdfText'

type Options = {
  docRef: React.MutableRefObject<any>
  activeKey: string
}

// Extractions pipeline through the pdf.js worker; a few in flight keeps whole-
// document scans off the one-await-per-page path without flooding the worker.
const FETCH_CONCURRENCY = 6

// Lazily extracts and caches the normalized text of each page from the
// pdf.js document.  One source of truth for locate, search, and any other
// whole-document text matching, so repeated lookups do not re-hit the worker.
export function usePageText({ docRef, activeKey }: Options) {
  const cacheRef = useRef<Map<number, string>>(new Map())
  const pendingRef = useRef<Map<number, Promise<string>>>(new Map())

  useEffect(() => {
    cacheRef.current.clear()
    pendingRef.current.clear()
  }, [activeKey])

  const getPageText = useCallback(
    (page: number): Promise<string> => {
      const cached = cacheRef.current.get(page)
      if (cached !== undefined) return Promise.resolve(cached)
      const pending = pendingRef.current.get(page)
      if (pending) return pending
      const doc = docRef.current
      if (!doc) return Promise.resolve('')
      const task = doc
        .getPage(page)
        .then((pdfPage: any) => pdfPage.getTextContent())
        .then((content: any) => {
          const text = normalized(
            (content.items || []).map((item: any) => item.str || '').join(' ')
          )
          // The document may have changed while the worker extracted; text
          // from the old one must not land in the new one's cache.
          if (docRef.current === doc) cacheRef.current.set(page, text)
          pendingRef.current.delete(page)
          return text
        })
        .catch(() => {
          pendingRef.current.delete(page)
          return ''
        })
      pendingRef.current.set(page, task)
      return task
    },
    [docRef]
  )

  // Reads every page with bounded concurrency instead of one await per page,
  // which is what made whole-document scans slow on long files.
  const getAllPageTexts = useCallback(
    async (numPages: number): Promise<string[]> => {
      const texts = new Array<string>(numPages).fill('')
      let next = 1
      const lane = async () => {
        while (next <= numPages) {
          const page = next
          next += 1
          texts[page - 1] = await getPageText(page)
        }
      }
      await Promise.all(
        Array.from({ length: Math.min(FETCH_CONCURRENCY, numPages) }, () => lane())
      )
      return texts
    },
    [getPageText]
  )

  return { getPageText, getAllPageTexts }
}
