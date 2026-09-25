import type { Match } from './pdfText'

export const MAX_MATCHES = 500

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
  for (let page = 1; page <= numPages && found.length < MAX_MATCHES; page += 1) {
    const text = await getPageText(page)
    if (!isCurrent()) return null
    if (!text) continue
    let at = text.indexOf(needle)
    while (at >= 0 && found.length < MAX_MATCHES) {
      found.push({ page, start: at, length: needle.length })
      at = text.indexOf(needle, at + 1)
    }
  }
  return isCurrent() ? found : null
}
