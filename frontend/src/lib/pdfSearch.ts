import type { Match } from './pdfText'

export const MAX_MATCHES = 500

// Pages are fetched in batches: one await per page serialized the scan on the
// pdf.js worker round trip, which dominated search time on long documents.
const FETCH_BATCH = 6

// Collects every occurrence of the caller-normalized `needle` (see
// `normalized`) in the cached page texts.  Returns null as soon as
// `isCurrent()` reports a newer search, so a slow scan can never publish
// results over the one that replaced it.
export async function collectMatches(
  needle: string,
  numPages: number,
  getPageText: (page: number) => Promise<string>,
  isCurrent: () => boolean
): Promise<Match[] | null> {
  if (!needle) return null
  const found: Match[] = []
  for (let start = 1; start <= numPages && found.length < MAX_MATCHES; start += FETCH_BATCH) {
    const end = Math.min(numPages, start + FETCH_BATCH - 1)
    const texts = await Promise.all(
      Array.from({ length: end - start + 1 }, (_, i) => getPageText(start + i))
    )
    if (!isCurrent()) return null
    for (let i = 0; i < texts.length && found.length < MAX_MATCHES; i += 1) {
      const text = texts[i]
      if (!text) continue
      const page = start + i
      let at = text.indexOf(needle)
      while (at >= 0 && found.length < MAX_MATCHES) {
        found.push({ page, start: at, length: needle.length })
        at = text.indexOf(needle, at + 1)
      }
    }
  }
  return isCurrent() ? found : null
}
