import {
  forwardRef,
  useEffect,
  useLayoutEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState
} from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { PDF_DOCUMENT_OPTIONS } from '../lib/pdfDocumentOptions'
import { buildSpanIndex, locateNeedle, normalized, paintRange, prefixMatchScore, type SpanIndex } from '../lib/pdfText'
import { applyDarkPageFilter } from '../lib/pdfDarkMode'
import type { AnnotationItem as ApiAnnotationItem } from '../lib/api'
import { usePageText } from '../hooks/usePageText'
import { usePdfZoom } from '../hooks/usePdfZoom'
import { usePdfSearch } from '../hooks/usePdfSearch'
import {
  ChevronLeft,
  ChevronRight,
  Download,
  FileText,
  Maximize2,
  Rows3,
  X,
  ZoomIn,
  ZoomOut
} from 'lucide-react'

export type AnnotationItem = ApiAnnotationItem

export type DownloadItem = {
  title: string
  label: string
  href?: string
  name?: string
}

type Props = {
  title: string
  pdfUrl?: string
  downloads?: DownloadItem[]
  counterpartLabel?: string
  onLocateCounterpart?: (payload: {
    selectedText: string
    page: number
    pageCount: number
  }) => void
  annotations?: AnnotationItem[]
  onCreateAnnotation?: (payload: {
    page: number
    quote: string
    color: string
    note: string
    positionRatio: number
  }) => Promise<void> | void
  onDeleteAnnotation?: (id: string) => Promise<void> | void
  onExportNotes?: () => void
  initialPosition?: { page: number; ratio: number } | null
  onProgressChange?: (page: number, ratio: number) => void
  onActivate?: () => void
}

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl

export type PdfPaneHandle = {
  locateAndHighlight: (payload: {
    text: string
    highlightText?: string
    positionRatio: number
  }) => Promise<void>
  goToPage: (page: number) => void
  openSearch: () => void
}

type ViewMode = 'scroll' | 'single'

const OVERLAY_CLASSES = [
  'pdf-text-highlight',
  'pdf-search-highlight',
  'pdf-search-current',
  'pdf-annotation-highlight',
  'pdf-annotation-yellow',
  'pdf-annotation-green',
  'pdf-annotation-blue',
  'pdf-annotation-pink'
]

const ANNOTATION_COLORS = ['yellow', 'green', 'blue', 'pink']

export const PdfPane = forwardRef<PdfPaneHandle, Props>(function PdfPane({
  title,
  pdfUrl,
  downloads,
  counterpartLabel,
  onLocateCounterpart,
  annotations = [],
  onCreateAnnotation,
  onDeleteAnnotation,
  onExportNotes,
  initialPosition,
  onProgressChange,
  onActivate
}: Props, ref) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const pageRefs = useRef<Array<HTMLDivElement | null>>([])
  const isProgrammaticScrollRef = useRef(false)
  const pdfDocumentRef = useRef<any>(null)
  const scaleRef = useRef(1.0)
  const zoomStackRef = useRef<HTMLDivElement | null>(null)
  const pageRatiosRef = useRef<Array<number | null>>([])
  const [numPages, setNumPages] = useState(0)
  const [ratioTick, setRatioTick] = useState(0)
  const [pageNumber, setPageNumber] = useState(1)
  const [scale, setScale] = useState(1.0)
  const [zoomInput, setZoomInput] = useState('100')
  const [containerWidth, setContainerWidth] = useState<number | undefined>(undefined)
  const [mode, setMode] = useState<ViewMode>('scroll')
  const [selectionMenu, setSelectionMenu] = useState<{
    x: number
    y: number
    text: string
    page: number
    canClearHighlight: boolean
    annotationId: string | null
  } | null>(null)
  const [noteDraft, setNoteDraft] = useState('')
  const [counterpart, setCounterpart] = useState<{
    page: number
    text: string
    highlight: string
  } | null>(null)
  const [renderRange, setRenderRange] = useState<{ start: number; end: number }>({ start: 1, end: 1 })
  const [notesOpen, setNotesOpen] = useState(false)
  const [activeAnnotationId, setActiveAnnotationId] = useState<string | null>(null)
  const [locateMessage, setLocateMessage] = useState('')
  const [darkMode, setDarkMode] = useState(() => document.documentElement.dataset.theme === 'dark')
  const centerCounterpartRef = useRef(false)

  const annotationsRef = useRef<AnnotationItem[]>(annotations)
  annotationsRef.current = annotations
  const counterpartRef = useRef(counterpart)
  counterpartRef.current = counterpart
  const centerCurrentMatchRef = useRef(false)
  const progressTimerRef = useRef<ReturnType<typeof setTimeout> | 0>(0)
  const progressValueRef = useRef<{ page: number; ratio: number } | null>(null)
  const restoredPositionRef = useRef(false)
  const renderWaitersRef = useRef<Map<number, Array<() => void>>>(new Map())
  const pageRenderedRef = useRef(handlePageRendered)
  pageRenderedRef.current = handlePageRendered
  // react-pdf rebuilds the text layer when this callback changes, clearing selections.
  const textLayerCallbacks = useMemo(
    () => Array.from({ length: numPages }, (_, index) => () => pageRenderedRef.current(index + 1)),
    [numPages]
  )
  const darkModeRef = useRef(darkMode)
  darkModeRef.current = darkMode
  const canvasRenderedRef = useRef(handleCanvasRendered)
  canvasRenderedRef.current = handleCanvasRendered
  const canvasCallbacks = useMemo(
    () => Array.from({ length: numPages }, (_, index) => () => canvasRenderedRef.current(index + 1)),
    [numPages]
  )

  useEffect(() => {
    const observer = new MutationObserver(() => {
      setDarkMode(document.documentElement.dataset.theme === 'dark')
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  const { getPageText } = usePageText({ docRef: pdfDocumentRef, activeKey: pdfUrl || '' })

  const updateRenderRange = () => {
    const scroller = scrollRef.current
    if (!scroller || mode !== 'scroll' || !numPages) return
    const top = scroller.scrollTop - 700
    const bottom = scroller.scrollTop + scroller.clientHeight + 700
    let first = numPages
    let last = 1
    for (let i = 0; i < pageRefs.current.length; i += 1) {
      const el = pageRefs.current[i]
      if (!el) continue
      const elTop = el.offsetTop
      const elBottom = elTop + el.offsetHeight
      if (elBottom >= top && elTop <= bottom) {
        first = Math.min(first, i + 1)
        last = Math.max(last, i + 1)
      }
    }
    if (first > last) return
    const start = Math.max(1, first - 1)
    const end = Math.min(numPages, last + 1)
    setRenderRange((prev) => (prev.start === start && prev.end === end ? prev : { start, end }))
  }

  const search = usePdfSearch({
    getPageText,
    numPages,
    goto: (page) => {
      centerCurrentMatchRef.current = true
      gotoPage(page)
    },
    onRepaint: () => repaintRenderedPages()
  })

  usePdfZoom({
    scrollerRef: scrollRef,
    zoomStackRef,
    scaleRef,
    scale,
    setScale,
    activeKey: pdfUrl || ''
  })

  useEffect(() => {
    setPageNumber(1)
    setNumPages(0)
    setScale(1.0)
    setZoomInput('100')
    scaleRef.current = 1.0
    pdfDocumentRef.current = null
    pageRefs.current = []
    pageRatiosRef.current = []
    renderWaitersRef.current.clear()
    setCounterpart(null)
    counterpartRef.current = null
    setNotesOpen(false)
    setActiveAnnotationId(null)
    setLocateMessage('')
    setRenderRange({ start: 1, end: 1 })
    if (progressTimerRef.current) clearTimeout(progressTimerRef.current)
    progressTimerRef.current = 0
    progressValueRef.current = null
    restoredPositionRef.current = false
    if (zoomStackRef.current) {
      zoomStackRef.current.style.transform = ''
      zoomStackRef.current.style.willChange = ''
    }
  }, [pdfUrl])

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setContainerWidth(Math.max(200, entry.contentRect.width - 24))
      }
    })
    ro.observe(el)
    return () => ro.disconnect()
    // The .pdf-body element only exists once a URL is set; on a fresh load the
    // first mount renders the empty branch, so the observer must re-attach
    // when the PDF actually appears (otherwise fit-width stays broken).
  }, [pdfUrl])

  useEffect(() => {
    scaleRef.current = scale
    setZoomInput(String(Math.round(scale * 100)))
    updateRenderRange()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scale])

  const fileOpts = useMemo(() => (pdfUrl ? { url: pdfUrl, withCredentials: true } : null), [pdfUrl])

  async function onDocumentLoadSuccess(doc: any) {
    pdfDocumentRef.current = doc
    setNumPages(doc.numPages)
    pageRefs.current = new Array(doc.numPages).fill(null)
    pageRatiosRef.current = new Array(doc.numPages).fill(null)
    // Page aspect ratios come from the page dictionaries (no rendering), so
    // the wrappers can be sized deterministically from the very first render
    // onward — independent of when canvas draws complete.
    doc.getPage(1).then((first: any) => {
      const fallback = first.view[3] / first.view[2]
      Promise.all(
        Array.from({ length: doc.numPages }, (_, i) =>
          doc.getPage(i + 1).then((p: any) => p.view[3] / p.view[2]).catch(() => fallback)
        )
      ).then((ratios: number[]) => {
        if (pdfDocumentRef.current === doc) {
          pageRatiosRef.current = ratios
          setRatioTick((t) => t + 1)
        }
      })
    }).catch(() => {})
  }

  function gotoPage(p: number) {
    if (!numPages) return
    const target = Math.max(1, Math.min(numPages, p))
    setPageNumber(target)
    if (mode === 'scroll') {
      const el = pageRefs.current[target - 1]
      const scroller = scrollRef.current
      if (el && scroller) {
        isProgrammaticScrollRef.current = true
        scroller.scrollTo({ top: el.offsetTop - 8, behavior: 'instant' })
        updateRenderRange()
        window.setTimeout(() => {
          isProgrammaticScrollRef.current = false
        }, 600)
      }
    }
  }

  function clearOverlayClasses(pageEl: HTMLElement) {
    pageEl.querySelectorAll(OVERLAY_CLASSES.map((c) => `.${c}`).join(',')).forEach((node) => {
      const el = node as HTMLElement
      for (const cls of OVERLAY_CLASSES) el.classList.remove(cls)
    })
    pageEl.querySelectorAll('[data-annotation-id]').forEach((node) => {
      delete (node as HTMLElement).dataset.annotationId
    })
  }

  function paintCounterpart(pageEl: HTMLElement, index: SpanIndex, payload: { text: string; highlight: string }) {
    const highlightNeedle = payload.highlight.trim()
    if (highlightNeedle) {
      const at = index.text.indexOf(normalized(highlightNeedle))
      if (at >= 0) {
        paintRange(index, at, at + normalized(highlightNeedle).length, 'pdf-text-highlight')
        return true
      }
    }
    const range = locateNeedle(index, payload.text)
    if (range) {
      paintRange(index, range.start, range.start + range.length, 'pdf-text-highlight')
      return true
    }
    // A title/caption is sometimes emitted as one large span.  Keep a narrow
    // single-span fallback instead of highlighting unrelated fragments.
    let fallbackLength = 0
    const target = normalized(payload.text)
    for (const entry of index.spans) {
      const value = normalized(entry.el.textContent || '')
      if (value.length >= 6 && target.includes(value) && value.length > fallbackLength) {
        fallbackLength = value.length
      }
    }
    if (fallbackLength >= 6) {
      for (const entry of index.spans) {
        const value = normalized(entry.el.textContent || '')
        if (value.length === fallbackLength && target.includes(value)) {
          entry.el.classList.add('pdf-text-highlight')
          return true
        }
      }
    }
    return false
  }

  function paintQuote(
    pageEl: HTMLElement,
    index: SpanIndex,
    quote: string,
    id: string,
    classNames: string
  ): boolean {
    const needle = normalized(quote)
    if (!needle) return false
    let at = index.text.indexOf(needle)
    let length = needle.length
    if (at < 0) {
      const range = locateNeedle(index, quote)
      if (!range) return false
      at = range.start
      length = range.length
    }
    paintRange(index, at, at + length, classNames)
    for (const entry of index.spans) {
      if (entry.end <= at || entry.start >= at + length) continue
      if (!entry.el.dataset.annotationId) entry.el.dataset.annotationId = id
    }
    return true
  }

  function applyOverlays(page: number, attempt = 0) {
    const pageEl = pageRefs.current[page - 1]
    if (!pageEl) return
    const index = buildSpanIndex(pageEl)
    if (!index) {
      // Text layer may still be mounting after the canvas render callback.
      if (attempt < 15) window.setTimeout(() => applyOverlays(page, attempt + 1), 150)
      return
    }
    clearOverlayClasses(pageEl)
    pageEl.classList.remove('pdf-page-counterpart-highlight')

    const cp = counterpartRef.current
    let counterpartMissing = false
    if (cp && cp.page === page) {
      counterpartMissing = !paintCounterpart(pageEl, index, cp)
      if (!counterpartMissing && centerCounterpartRef.current) {
        centerCounterpartRef.current = false
        isProgrammaticScrollRef.current = true
        pageEl.querySelector('.pdf-text-highlight')?.scrollIntoView({ block: 'center', behavior: 'instant' })
        window.setTimeout(() => { isProgrammaticScrollRef.current = false }, 600)
      }
    }

    const searchState = search.stateRef.current
    if (searchState.open && searchState.matches.length) {
      for (let i = 0; i < searchState.matches.length; i += 1) {
        const match = searchState.matches[i]
        if (match.page !== page) continue
        if (i === searchState.current) {
          paintRange(index, match.start, match.start + match.length, 'pdf-search-highlight pdf-search-current')
        } else {
          paintRange(index, match.start, match.start + match.length, 'pdf-search-highlight')
        }
      }
      const currentMatch = searchState.matches[searchState.current]
      if (centerCurrentMatchRef.current && currentMatch?.page === page) {
        centerCurrentMatchRef.current = false
        const entry = index.spans.find((s) => currentMatch.start < s.end && currentMatch.start >= s.start)
        entry?.el.scrollIntoView({ block: 'center' })
      }
    }

    for (const annotation of annotationsRef.current) {
      if (annotation.page !== page || !annotation.quote) continue
      paintQuote(
        pageEl,
        index,
        annotation.quote,
        annotation.id,
        `pdf-annotation-highlight pdf-annotation-${annotation.color}`
      )
    }

    if (counterpartMissing) setLocateMessage('未在此页找到对应文字，请选择一句完整文本后重试。')
  }

  function repaintRenderedPages() {
    for (let i = 0; i < pageRefs.current.length; i += 1) {
      const el = pageRefs.current[i]
      if (el?.querySelector('.react-pdf__Page__textContent')) applyOverlays(i + 1)
    }
  }

  function handlePageRendered(page: number) {
    const waiters = renderWaitersRef.current.get(page)
    if (waiters) {
      renderWaitersRef.current.delete(page)
      for (const resolve of waiters) resolve()
    }
    applyOverlays(page)
  }

  function handleCanvasRendered(page: number) {
    if (!darkModeRef.current) return
    const canvas = pageRefs.current[page - 1]?.querySelector('canvas')
    if (canvas) applyDarkPageFilter(canvas)
  }

  function whenPageRendered(page: number): Promise<void> {
    const el = pageRefs.current[page - 1]
    if (el?.querySelector('.react-pdf__Page__textContent span')) return Promise.resolve()
    return new Promise((resolve) => {
      const list = renderWaitersRef.current.get(page) || []
      list.push(resolve)
      renderWaitersRef.current.set(page, list)
      window.setTimeout(resolve, 8000)
    })
  }

  function clearHighlights() {
    counterpartRef.current = null
    centerCounterpartRef.current = false
    setCounterpart(null)
    const pane = containerRef.current
    if (!pane) return
    pane.querySelectorAll('.pdf-text-highlight').forEach((node) => node.classList.remove('pdf-text-highlight'))
    pane.querySelectorAll('.pdf-page-counterpart-highlight').forEach((node) => node.classList.remove('pdf-page-counterpart-highlight'))
  }

  function commitZoomInput() {
    const parsed = Number.parseFloat(zoomInput.replace('%', '').trim())
    if (!Number.isFinite(parsed)) {
      setZoomInput(String(Math.round(scaleRef.current * 100)))
      return
    }
    const percent = Math.max(40, Math.min(300, Math.round(parsed)))
    const nextScale = percent / 100
    scaleRef.current = nextScale
    setScale(nextScale)
    setZoomInput(String(percent))
  }

  async function locateAndHighlight({ text, highlightText, positionRatio }: {
    text: string; highlightText?: string; positionRatio: number
  }) {
      const doc = pdfDocumentRef.current
      if (!doc || !numPages) { setLocateMessage('PDF 正在加载，请稍后重试。'); return }
      setLocateMessage('正在定位…')
      const hint = Math.max(1, Math.min(numPages, Math.round(positionRatio * Math.max(0, numPages - 1)) + 1))
      // Score every page by the longest prefix of the target it contains;
      // ties and misses fall back to the position hint.  Whole-document scan
      // is cheap because page texts are cached.
      const highlightTarget = normalized(highlightText || '')
      const blockTarget = normalized(text)
      let bestPage = hint
      let bestScore = 0
      let bestHighlightScore = 0
      for (let page = 1; page <= numPages; page += 1) {
        const pageText = await getPageText(page)
        const highlightScore = prefixMatchScore(pageText, highlightTarget)
        const score = prefixMatchScore(pageText, blockTarget)
        if (highlightScore > bestHighlightScore || (highlightScore === bestHighlightScore &&
          (score > bestScore || (score === bestScore && Math.abs(page - hint) < Math.abs(bestPage - hint))))) {
          bestHighlightScore = highlightScore
          bestScore = score
          bestPage = page
        }
      }
      if (pdfDocumentRef.current !== doc) return
      if (!bestScore && !bestHighlightScore) {
        setLocateMessage('未找到匹配文字，请选择一句完整文本后重试。')
        return
      }
      setLocateMessage('')
      const next = { page: bestPage, text, highlight: highlightText || '' }
      counterpartRef.current = next
      centerCounterpartRef.current = true
      setCounterpart(next)
      gotoPage(bestPage)
      await whenPageRendered(bestPage)
      applyOverlays(bestPage)
  }

  useImperativeHandle(ref, () => ({
    locateAndHighlight,
    goToPage(page: number) {
      const target = Math.max(1, Math.min(numPages || 1, Math.round(page)))
      if (mode !== 'scroll') {
        setPageNumber(target)
        return
      }
      const scroller = scrollRef.current
      const el = pageRefs.current[target - 1]
      if (!scroller) return
      isProgrammaticScrollRef.current = true
      if (el) {
        scroller.scrollTop = el.offsetTop - 8
      }
      updateRenderRange()
      window.setTimeout(() => {
        isProgrammaticScrollRef.current = false
      }, 200)
    },
    openSearch() {
      search.openSearch()
    }
  }))

  function handleTextMouseDown(event: React.MouseEvent) {
    if (event.button !== 2 && !(event.button === 0 && event.ctrlKey)) return
    const selection = window.getSelection()
    if (selection?.toString().trim() &&
      scrollRef.current?.contains(selection.anchorNode) &&
      scrollRef.current.contains(selection.focusNode)) {
      event.preventDefault()
      event.stopPropagation()
    }
  }

  function handleTextContextMenu(event: React.MouseEvent) {
    const target = event.target as HTMLElement
    const highlighted = Boolean(
      target.closest('.pdf-text-highlight') || target.closest('.pdf-page-counterpart-highlight')
    )
    const annotationEl = target.closest<HTMLElement>('[data-annotation-id]')
    const selection = window.getSelection()
    const text = selection?.toString().trim() || ''
    const selectedInPane = Boolean(text && containerRef.current?.contains(selection?.anchorNode ?? null))
    if (!selectedInPane && !highlighted && !annotationEl) return
    const pageElement = target.closest<HTMLElement>('[data-pdf-page]')
    const selectedPage = Number(pageElement?.dataset.pdfPage || pageNumber)
    event.preventDefault()
    event.stopPropagation()
    setNoteDraft('')
    setSelectionMenu({
      x: event.clientX,
      y: event.clientY,
      text: selectedInPane ? text.slice(0, 2000) : '',
      page: selectedPage,
      canClearHighlight: highlighted,
      annotationId: annotationEl?.dataset.annotationId || null
    })
  }

  async function submitAnnotation(color: string) {
    const menu = selectionMenu
    if (!menu || !onCreateAnnotation || !menu.text) return
    setSelectionMenu(null)
    window.getSelection()?.removeAllRanges()
    await onCreateAnnotation({
      page: menu.page,
      quote: menu.text,
      color,
      note: noteDraft.trim(),
      positionRatio: numPages > 1 ? (menu.page - 1) / (numPages - 1) : 0
    })
  }

  // Track current page in scroll mode by detecting which page is closest to
  // top; the same handler drives virtualization and reading-progress
  // reporting.
  useEffect(() => {
    if (mode !== 'scroll' || !numPages) return
    const scroller = scrollRef.current
    if (!scroller) return

    const emitProgress = (page: number, ratio: number) => {
      progressValueRef.current = { page, ratio }
      if (progressTimerRef.current) clearTimeout(progressTimerRef.current)
      progressTimerRef.current = setTimeout(() => {
        progressTimerRef.current = 0
        if (progressValueRef.current) {
          onProgressChange?.(progressValueRef.current.page, progressValueRef.current.ratio)
        }
      }, 2000)
    }

    const handler = () => {
      const top = scroller.scrollTop + 40
      let current = 1
      for (let i = 0; i < pageRefs.current.length; i += 1) {
        const el = pageRefs.current[i]
        if (!el) continue
        if (el.offsetTop <= top) current = i + 1
        else break
      }
      setPageNumber((prev) => (prev === current ? prev : current))
      updateRenderRange()
      if (isProgrammaticScrollRef.current) return
      const denominator = Math.max(1, scroller.scrollHeight - scroller.clientHeight)
      const ratio = Math.max(0, Math.min(1, scroller.scrollTop / denominator))
      emitProgress(current, ratio)
    }
    scroller.addEventListener('scroll', handler, { passive: true })
    return () => {
      scroller.removeEventListener('scroll', handler)
      if (progressTimerRef.current) {
        clearTimeout(progressTimerRef.current)
        progressTimerRef.current = 0
        if (progressValueRef.current) {
          onProgressChange?.(progressValueRef.current.page, progressValueRef.current.ratio)
        }
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, numPages, onProgressChange])

  // Recompute the rendered window when layout geometry changes without a
  // user scroll (document load, zoom, pane resize).
  useLayoutEffect(() => {
    updateRenderRange()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [numPages, mode, scale, containerWidth, ratioTick])

  // Restore the saved reading position once, after the page wrappers have
  // deterministic sizes.
  useLayoutEffect(() => {
    if (restoredPositionRef.current || !numPages || !initialPosition) return
    if (!initialPosition.page && !initialPosition.ratio) {
      restoredPositionRef.current = true
      return
    }
    restoredPositionRef.current = true
    const target = Math.max(1, Math.min(numPages, Math.round(initialPosition.page) || 1))
    if (mode === 'single') {
      setPageNumber(target)
      return
    }
    const scroller = scrollRef.current
    const el = pageRefs.current[target - 1]
    if (!scroller) return
    isProgrammaticScrollRef.current = true
    if (el && target > 1) {
      scroller.scrollTop = el.offsetTop - 8
    } else if (initialPosition.ratio > 0.01) {
      scroller.scrollTop = initialPosition.ratio * Math.max(0, scroller.scrollHeight - scroller.clientHeight)
    }
    updateRenderRange()
    window.setTimeout(() => {
      isProgrammaticScrollRef.current = false
    }, 400)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [numPages, mode, initialPosition, ratioTick])

  // Repaint overlays when the annotation set changes (create/delete/sync).
  useEffect(() => {
    repaintRenderedPages()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [annotations, counterpart])

  // Repaint after React commits new search state; painting reads the ref that
  // only updates on render, so calling it from the search callback directly
  // would paint the previous match set.
  const searchStateKey = `${search.open}|${search.query}|${search.matches.length}|${search.current}`
  useEffect(() => {
    repaintRenderedPages()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchStateKey])

  // Capture each page's intrinsic aspect ratio once (from the PDF page
  // dictionaries at load) so the page wrappers can be sized synchronously on
  // every scale change; without stable sizes the layout collapses while
  // react-pdf redraws the canvases asynchronously, which used to yank the
  // scroll position on every zoom step.
  const pageSlotStyle = (
    pageIndex: number,
    extraHeight = 0
  ): React.CSSProperties | undefined => {
    void ratioTick // ratios arrive async; bump forces wraps to re-render sized
    const ratio = pageRatiosRef.current[pageIndex]
    if (!ratio || !containerWidth) return undefined
    const width = containerWidth * scale
    return { width, height: width * ratio + extraHeight }
  }

  if (!pdfUrl) {
    return (
      <div className="pdf-pane">
        <div className="pdf-toolbar">
          <div className="pdf-title">{title}</div>
        </div>
        <div className="pdf-empty muted">暂无 PDF</div>
      </div>
    )
  }

  const renderPage = (index: number, extraHeight: number, withLabel: boolean) => {
    const page = index + 1
    const shouldRender =
      mode === 'single'
        ? page === pageNumber
        : page >= renderRange.start && page <= renderRange.end
    return (
      <div
        key={`page-${page}`}
        className="pdf-page-wrap"
        data-pdf-page={page}
        style={pageSlotStyle(index, extraHeight)}
        ref={(el) => {
          pageRefs.current[index] = el
        }}
      >
        {shouldRender ? (
          <Page
            key={`${page}-${darkMode ? 'dark' : 'light'}`}
            pageNumber={page}
            scale={scale}
            width={containerWidth}
            renderTextLayer
            renderAnnotationLayer
            onRenderSuccess={canvasCallbacks[index]}
            onRenderTextLayerSuccess={textLayerCallbacks[index]}
          />
        ) : (
          <div className="pdf-page-placeholder">
            <span>{page}</span>
          </div>
        )}
        {withLabel && <div className="pdf-page-label muted small">第 {page} 页</div>}
      </div>
    )
  }

  return (
    <div
      className="pdf-pane"
      onMouseDownCapture={() => onActivate?.()}
    >
      <div className="pdf-toolbar">
        <div className="pdf-title" title={title}>
          {title}
        </div>
        <div className="pdf-controls">
          <button className="icon-btn" title="上一页" onClick={() => gotoPage(pageNumber - 1)}>
            <ChevronLeft size={16} />
          </button>
          <input
            className="page-input"
            type="text"
            inputMode="numeric"
            value={pageNumber}
            onChange={(e) => {
              const value = e.target.value.replace(/[^0-9]/g, '')
              if (value) gotoPage(parseInt(value, 10))
            }}
          />
          <span className="muted small">/ {numPages || '—'}</span>
          <button className="icon-btn" title="下一页" onClick={() => gotoPage(pageNumber + 1)}>
            <ChevronRight size={16} />
          </button>
          <span className="sep" />
          <button className="icon-btn" title="缩小" onClick={() => setScale((s) => Math.max(0.4, s - 0.1))}>
            <ZoomOut size={16} />
          </button>
          <label className="zoom-input-wrap" title="手动输入缩放比例（40%–300%）">
            <input
              className="zoom-input"
              type="text"
              inputMode="numeric"
              aria-label="缩放百分比"
              value={zoomInput}
              onChange={(event) => setZoomInput(event.target.value.replace(/[^0-9.%]/g, ''))}
              onBlur={commitZoomInput}
              onFocus={(event) => event.currentTarget.select()}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  commitZoomInput()
                  event.currentTarget.blur()
                } else if (event.key === 'Escape') {
                  setZoomInput(String(Math.round(scaleRef.current * 100)))
                  event.currentTarget.blur()
                }
              }}
            />
            <span>%</span>
          </label>
          <button className="icon-btn" title="放大" onClick={() => setScale((s) => Math.min(3, s + 0.1))}>
            <ZoomIn size={16} />
          </button>
          <button className="icon-btn" title="适合宽度" onClick={() => setScale(1.0)}>
            <Maximize2 size={16} />
          </button>
          <span className="sep" />
          <button
            className={`icon-btn ${mode === 'scroll' ? 'active' : ''}`}
            title="滚动阅读"
            onClick={() => setMode('scroll')}
          >
            <Rows3 size={16} />
          </button>
          <button
            className={`icon-btn ${mode === 'single' ? 'active' : ''}`}
            title="单页模式"
            onClick={() => setMode('single')}
          >
            <FileText size={16} />
          </button>
          {(downloads && downloads.length > 0 ? downloads : [{ title: '下载', label: '', href: pdfUrl }]).map((item) => (
            <a
              key={item.title}
              className={`icon-btn download-btn${item.href ? '' : ' disabled'}`}
              title={item.title}
              href={item.href}
              download={item.name || true}
              aria-disabled={!item.href}
              onClick={item.href ? undefined : (event) => event.preventDefault()}
            >
              <Download size={16} />
              {item.label && <span>{item.label}</span>}
            </a>
          ))}
        </div>
      </div>

      <div className="pdf-body" ref={containerRef}>
        {locateMessage && <div className="pdf-locate-message" role="status">{locateMessage}<button className="icon-btn" title="关闭定位提示" onClick={() => setLocateMessage('')}><X size={14} /></button></div>}
        {search.open && (
          <div className="pdf-search-bar">
            <input
              autoFocus
              className="pdf-search-input"
              type="text"
              placeholder="在文档中搜索…"
              value={search.query}
              onChange={(event) => search.updateQuery(event.target.value)}
            />
            <span className="pdf-search-count muted small">
              {search.searching ? '搜索中…' : search.matches.length ? `${search.current + 1}/${search.matches.length}` : (search.query ? '无结果' : '')}
            </span>
            <button className="icon-btn" title="上一个 (Shift+Enter)" onClick={search.prev} disabled={!search.matches.length}>
              <ChevronLeft size={14} />
            </button>
            <button className="icon-btn" title="下一个 (Enter)" onClick={search.next} disabled={!search.matches.length}>
              <ChevronRight size={14} />
            </button>
            <button className="icon-btn" title="关闭 (Esc)" onClick={search.closeSearch}>
              <X size={14} />
            </button>
          </div>
        )}
        <div className="pdf-canvas-wrap" ref={scrollRef} onContextMenu={handleTextContextMenu}
          onMouseDownCapture={handleTextMouseDown}
          onClick={(event) => {
            if (window.getSelection()?.toString().trim()) return
            const id = (event.target as HTMLElement).closest<HTMLElement>('[data-annotation-id]')?.dataset.annotationId
            if (id) { setActiveAnnotationId(id); setNotesOpen(true) }
          }}>
          <Document
            file={fileOpts ?? undefined}
            options={PDF_DOCUMENT_OPTIONS}
            externalLinkTarget="_blank"
            externalLinkRel="noopener noreferrer"
            onLoadSuccess={onDocumentLoadSuccess}
            loading={<div className="muted" style={{ padding: 20 }}>加载中…</div>}
            error={<div className="muted" style={{ padding: 20 }}>无法加载 PDF</div>}
          >
            {numPages > 0 && mode === 'single' && (
              <div
                className="pdf-page-wrap pdf-zoom-stack"
                data-pdf-page={pageNumber}
                style={pageSlotStyle(pageNumber - 1)}
                ref={(el) => {
                  pageRefs.current[pageNumber - 1] = el
                  zoomStackRef.current = el
                }}
              >
                <Page
                  key={`${pageNumber}-${darkMode ? 'dark' : 'light'}`}
                  pageNumber={pageNumber}
                  scale={scale}
                  width={containerWidth}
                  renderTextLayer
                  renderAnnotationLayer
                  onRenderSuccess={canvasCallbacks[pageNumber - 1]}
                  onRenderTextLayerSuccess={textLayerCallbacks[pageNumber - 1]}
                />
              </div>
            )}
            {numPages > 0 && mode === 'scroll' && (
              <div className="pdf-scroll-stack" ref={(el) => { zoomStackRef.current = el }}>
                {Array.from({ length: numPages }, (_, i) => renderPage(i, 24, true))}
              </div>
            )}
          </Document>
        </div>
        {notesOpen && (
          <aside className="pdf-notes-panel" aria-label="批注笔记">
            <div className="pdf-overlay-heading"><strong>批注笔记 · {annotations.length}</strong><button className="icon-btn" title="关闭批注笔记" onClick={() => setNotesOpen(false)}><X size={14} /></button></div>
            <button className="btn" disabled={!annotations.length} onClick={() => onExportNotes?.()}>导出阅读笔记</button>
            {!annotations.length && <p className="muted">选择 PDF 文字，右键添加高亮和备注。</p>}
            {annotations.map((annotation) => (
              <article key={annotation.id} className={`pdf-note-card ${activeAnnotationId === annotation.id ? 'active' : ''}`}
                ref={(el) => { if (el && activeAnnotationId === annotation.id) el.scrollIntoView({ block: 'nearest' }) }}>
                <div className="pdf-overlay-heading"><span>第 {annotation.page} 页</span><span className={`menu-swatch swatch-${annotation.color}`} /></div>
                <blockquote>{annotation.quote}</blockquote>
                <p className="pdf-note-text">{annotation.note || '未填写备注'}</p>
                <div className="pdf-note-actions">
                  <button className="btn" onClick={() => { setActiveAnnotationId(annotation.id); void locateAndHighlight({ text: annotation.quote, positionRatio: annotation.position_ratio }) }}>定位原句</button>
                  {onDeleteAnnotation && <button className="btn" onClick={() => void onDeleteAnnotation(annotation.id)}>删除批注</button>}
                </div>
              </article>
            ))}
          </aside>
        )}
      </div>
      {selectionMenu && (
        <>
          <div className="context-menu-overlay" onClick={() => setSelectionMenu(null)} />
          <div className="context-menu pdf-selection-menu" style={{ top: selectionMenu.y, left: selectionMenu.x }}>
            {selectionMenu.text && (
              <>
                <button
                  className="context-menu-item"
                  onClick={() => {
                    const selected = selectionMenu
                    setSelectionMenu(null)
                    onLocateCounterpart?.({
                      selectedText: selected.text,
                      page: selected.page,
                      pageCount: numPages,
                    })
                  }}
                >
                  跳转到{counterpartLabel || '对应内容'}并高亮
                </button>
                {onCreateAnnotation && (
                  <div className="menu-annotation">
                    <div className="menu-annotation-colors">
                      {ANNOTATION_COLORS.map((color) => (
                        <button
                          key={color}
                          className={`menu-swatch swatch-${color}`}
                          title={`添加${color === 'yellow' ? '黄色' : color === 'green' ? '绿色' : color === 'blue' ? '蓝色' : '粉色'}高亮`}
                          onClick={() => void submitAnnotation(color)}
                        />
                      ))}
                    </div>
                    <input
                      className="menu-annotation-note"
                      type="text"
                      placeholder="备注（可选，Enter 保存黄色高亮）"
                      value={noteDraft}
                      onChange={(event) => setNoteDraft(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault()
                          void submitAnnotation('yellow')
                        }
                      }}
                    />
                  </div>
                )}
              </>
            )}
            {selectionMenu.annotationId && onDeleteAnnotation && (
              <button className="context-menu-item" onClick={() => {
                setActiveAnnotationId(selectionMenu.annotationId); setNotesOpen(true); setSelectionMenu(null)
              }}>查看批注</button>
            )}
            {selectionMenu.annotationId && onDeleteAnnotation && (
              <button
                className="context-menu-item"
                onClick={() => {
                  const id = selectionMenu.annotationId
                  setSelectionMenu(null)
                  if (id) void onDeleteAnnotation(id)
                }}
              >
                删除此批注
              </button>
            )}
            {selectionMenu.canClearHighlight && (
              <button
                className="context-menu-item"
                onClick={() => {
                  clearHighlights()
                  setSelectionMenu(null)
                  window.getSelection()?.removeAllRanges()
                }}
              >
                清除对照高亮
              </button>
            )}
          </div>
        </>
      )}
    </div>
  )
})
