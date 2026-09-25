import { forwardRef } from 'react'
import { PdfPane, type PdfPaneHandle, type PdfPaneProps } from './PdfPane'

export type CachedPane = { revision: number; pane: PdfPaneProps }
type Props = {
  activeId: string
  panes: Record<string, CachedPane>
  retainedIds: string[]
}

// Keep the most recent readers mounted, canvases included, so switching back
// to a cached paper only flips `active`. Each pane gets per-document props
// from the parent, so inactive entries still report progress to their own
// document. `revision` forces a remount when a document is reprocessed.
export const PdfPaneCache = forwardRef<PdfPaneHandle, Props>(function PdfPaneCache({
  activeId, panes, retainedIds
}, ref) {
  const ids = [...retainedIds.filter((id) => id !== activeId && panes[id]), activeId].slice(-3)
  return <>{ids.map((id) => {
    const entry = panes[id]
    return entry ? (
      <PdfPane
        {...entry.pane}
        key={`${id}:${entry.revision}`}
        active={id === activeId}
        ref={id === activeId ? ref : null}
      />
    ) : null
  })}</>
})
