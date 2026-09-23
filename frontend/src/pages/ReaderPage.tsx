import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, PanelLeftOpen, UploadCloud } from 'lucide-react'
import { PdfPane } from '../components/PdfPane'
import type { AnnotationItem, PdfPaneHandle } from '../components/PdfPane'
import type { FigureItem, UserSettings } from '../lib/api'
import type { OutlineItem } from '../lib/pdfOutline'
import { ProgressBar } from '../components/ProgressBar'
import { SettingsModal } from '../components/SettingsModal'
import { Sidebar } from '../components/Sidebar'
import type { DocumentStatus, DocumentSummary } from '../lib/api'
import {
  cancelDocument,
  createAnnotation,
  deleteAnnotation,
  deleteDocument,
  downloadNotes,
  ensureAnnotatedPdf,
  ensureMergedPdf,
  getDocumentStatus,
  getDocumentStructure,
  listAnnotations,
  listDocuments,
  locateCounterpart,
  makeDataUrl,
  mergedPdfName,
  renameDocument,
  reprocessDocument,
  retryDocument,
  translatedPdfName,
  updateReadingProgress,
  updateSettings,
  uploadFile
} from '../lib/api'

type PendingLocate = { text: string; side: 'original' | 'translated' } | null

type Props = {
  settings: UserSettings
  onSettingsChange: (settings: UserSettings) => void
}

export function ReaderPage({ settings, onSettingsChange }: Props) {
  const [summaries, setSummaries] = useState<DocumentSummary[]>([])
  const [activeId, setActiveId] = useState<string | undefined>(undefined)
  const [docCache, setDocCache] = useState<Record<string, DocumentStatus>>({})
  const [uploading, setUploading] = useState(false)
  const [showSidebar, setShowSidebar] = useState(() => window.innerWidth >= 900)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [theme, setTheme] = useState<UserSettings['theme']>(settings.theme)
  const [favorites, setFavorites] = useState<string[]>(settings.favorites)
  const [notice, setNotice] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [pollRevision, setPollRevision] = useState(0)
  const [annotations, setAnnotations] = useState<AnnotationItem[]>([])
  const [structureOutline, setStructureOutline] = useState<OutlineItem[] | null>(null)
  const [structureFigures, setStructureFigures] = useState<FigureItem[]>([])
  const [pendingLocate, setPendingLocate] = useState<PendingLocate>(null)

  const pollTimerRef = useRef<number | null>(null)
  const paneRef = useRef<PdfPaneHandle | null>(null)
  const emptyUploadRef = useRef<HTMLInputElement | null>(null)
  const annotatedRequestRef = useRef<string | null>(null)
  const mergedRequestRef = useRef<string | null>(null)

  useEffect(() => {
    setTheme(settings.theme)
    setFavorites(settings.favorites)
  }, [settings])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
  }, [theme])

  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth < 900) setShowSidebar(false)
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  const persistPreferences = useCallback(async (payload: Partial<UserSettings>) => {
    try {
      const nextSettings = await updateSettings(payload)
      onSettingsChange(nextSettings)
    } catch (e: any) {
      setNotice(`偏好保存失败：${e?.message || String(e)}`)
    }
  }, [onSettingsChange])

  const refreshActive = useCallback(() => {
    if (!activeId) return
    void getDocumentStatus(activeId)
      .then((d) => setDocCache((c) => ({ ...c, [activeId]: d })))
      .catch((e) => console.error(e))
  }, [activeId])

  const refreshSummaries = useCallback(async () => {
    try {
      const list = await listDocuments()
      setSummaries(list)
      return list
    } catch (e: any) {
      console.error(e)
      return []
    }
  }, [])

  useEffect(() => {
    void (async () => {
      const list = await refreshSummaries()
      if (list.length > 0) {
        setActiveId((prev) => prev && list.some((item) => item.document_id === prev) ? prev : list[0].document_id)
      }
    })()
  }, [refreshSummaries])

  useEffect(() => {
    if (pollTimerRef.current) {
      window.clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
    if (!activeId) return

    const fetchOnce = async () => {
      try {
        const data = await getDocumentStatus(activeId)
        setDocCache((c) => ({ ...c, [activeId]: data }))
        setSummaries((prev) =>
          prev.map((s) =>
            s.document_id === activeId
              ? {
                  ...s,
                  status: data.status,
                  has_translated_pdf: !!data.translated_pdf_url,
                  updated_at: data.updated_at,
                  last_opened_at: data.last_opened_at
                }
              : s
          )
        )
        if (data.status === 'done' || data.status === 'failed' || data.status === 'cancelled') {
          if (pollTimerRef.current) {
            window.clearInterval(pollTimerRef.current)
            pollTimerRef.current = null
          }
        }
      } catch (e: any) {
        console.error(e)
      }
    }
    void fetchOnce()
    pollTimerRef.current = window.setInterval(fetchOnce, 1500)
    return () => {
      if (pollTimerRef.current) {
        window.clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
    }
  }, [activeId, pollRevision])

  const activeDoc: DocumentStatus | undefined = activeId ? docCache[activeId] : undefined
  const translatedPdfUrl = activeDoc?.translated_pdf_url ? makeDataUrl(activeDoc.translated_pdf_url) : undefined
  const annotatedPdfUrl = activeDoc?.annotated_pdf_url ? makeDataUrl(activeDoc.annotated_pdf_url) : undefined
  const mergedPdfUrl = activeDoc?.merged_pdf_url ? makeDataUrl(activeDoc.merged_pdf_url) : undefined
  const showAnnotated = settings.show_annotated_pdf && Boolean(annotatedPdfUrl)
  const panePdfUrl = showAnnotated ? annotatedPdfUrl : mergedPdfUrl

  // Documents parsed before annotation existed have no artifact yet; build it
  // once from their cached parse when the preference is on.
  useEffect(() => {
    if (!settings.show_annotated_pdf) return
    if (!activeId || activeDoc?.status !== 'done' || activeDoc.annotated_pdf_url) return
    if (annotatedRequestRef.current === activeId) return
    annotatedRequestRef.current = activeId
    void ensureAnnotatedPdf(activeId)
      .then((url) => {
        setDocCache((cache) => {
          const current = cache[activeId]
          return current ? { ...cache, [activeId]: { ...current, annotated_pdf_url: url } } : cache
        })
      })
      .catch((error) => console.error(error))
  }, [settings.show_annotated_pdf, activeId, activeDoc?.status, activeDoc?.annotated_pdf_url])

  // Documents translated before the merged PDF existed get it built once from
  // their stored original + translation when they are opened.
  useEffect(() => {
    if (!activeId || activeDoc?.status !== 'done' || activeDoc.merged_pdf_url) return
    if (mergedRequestRef.current === activeId) return
    mergedRequestRef.current = activeId
    void ensureMergedPdf(activeId)
      .then((url) => {
        setDocCache((cache) => {
          const current = cache[activeId]
          return current ? { ...cache, [activeId]: { ...current, merged_pdf_url: url } } : cache
        })
      })
      .catch((error) => console.error(error))
  }, [activeId, activeDoc?.status, activeDoc?.merged_pdf_url])

  const handleRetry = useCallback(async () => {
    if (!activeId || retrying) return
    setRetrying(true)
    try {
      const queued = await retryDocument(activeId)
      setDocCache((cache) => ({
        ...cache,
        [activeId]: cache[activeId]
          ? { ...cache[activeId], status: queued.status, current_stage_label: `等待从 ${queued.resume_from} 恢复` }
          : cache[activeId]
      }))
      setSummaries((items) => items.map((item) => (
        item.document_id === activeId ? { ...item, status: 'queued' } : item
      )))
      setPollRevision((value) => value + 1)
    } catch (error: any) {
      setNotice(`重试失败：${error?.message ?? String(error)}`)
    } finally {
      setRetrying(false)
    }
  }, [activeId, retrying])

  const handleCancel = useCallback(async () => {
    if (!activeId || cancelling) return
    setCancelling(true)
    try {
      const cancelled = await cancelDocument(activeId)
      setDocCache((cache) => cache[activeId] ? { ...cache, [activeId]: { ...cache[activeId], status: cancelled.status } } : cache)
      setSummaries((items) => items.map((item) => item.document_id === activeId ? { ...item, status: cancelled.status } : item))
    } catch (error: any) {
      setNotice(`取消失败：${error?.message ?? String(error)}`)
    } finally {
      setCancelling(false)
    }
  }, [activeId, cancelling])

  const handleReprocess = useCallback(async (documentId: string) => {
    try {
      const queued = await reprocessDocument(documentId)
      setSummaries((items) => items.map((item) => (
        item.document_id === documentId ? { ...item, status: 'queued' } : item
      )))
      setDocCache((cache) => ({
        ...cache,
        [documentId]: cache[documentId]
          ? { ...cache[documentId], status: queued.status, current_stage_label: `等待重新处理（从 ${queued.resume_from} 开始）` }
          : cache[documentId]
      }))
      if (documentId === activeId) setPollRevision((value) => value + 1)
    } catch (error: any) {
      setNotice(`重新处理失败：${error?.message ?? String(error)}`)
    }
  }, [activeId])

  useEffect(() => {
    setAnnotations([])
    setStructureOutline(null)
    setStructureFigures([])
    setPendingLocate(null)
  }, [activeId])

  // Annotations and the document structure (backend outline + figure gallery)
  // are per-document; reload them whenever the active document changes and
  // again once its pipeline finishes.
  useEffect(() => {
    if (!activeId || activeDoc?.status !== 'done') return
    let cancelled = false
    void listAnnotations(activeId).then((items) => { if (!cancelled) setAnnotations(items) }).catch((e) => console.error(e))
    void getDocumentStructure(activeId)
      .then((structure) => {
        if (cancelled) return
        setStructureOutline(structure.outline?.length ? structure.outline : null)
        setStructureFigures(
          (structure.figures || []).map((figure) => ({ ...figure, url: makeDataUrl(figure.url) }))
        )
      })
      .catch((e) => console.error(e))
    return () => { cancelled = true }
  }, [activeId, activeDoc?.status])

  const handleCreateAnnotation = useCallback(async (payload: {
    page: number
    quote: string
    color: string
    note: string
    positionRatio: number
  }) => {
    if (!activeId) return
    try {
      await createAnnotation(activeId, {
        page: payload.page,
        quote: payload.quote,
        color: payload.color,
        note: payload.note,
        position_ratio: payload.positionRatio
      })
      setAnnotations(await listAnnotations(activeId))
    } catch (e: any) {
      setNotice(`批注保存失败：${e?.message ?? String(e)}`)
    }
  }, [activeId])

  const handleDeleteAnnotation = useCallback(async (id: string) => {
    if (!activeId) return
    try {
      await deleteAnnotation(activeId, id)
      setAnnotations((prev) => prev.filter((item) => item.id !== id))
    } catch (e: any) {
      setNotice(`批注删除失败：${e?.message ?? String(e)}`)
    }
  }, [activeId])

  const handleExportNotes = useCallback(() => {
    if (!activeId) return
    void downloadNotes(activeId).catch((e: any) => setNotice(`导出笔记失败：${e?.message ?? String(e)}`))
  }, [activeId])

  const handleProgressChange = useCallback((page: number, ratio: number) => {
    if (!activeId) return
    void updateReadingProgress(activeId, page, ratio).catch(() => {})
  }, [activeId])

  // Ctrl/Cmd+F opens in-document search.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'f') return
      event.preventDefault()
      paneRef.current?.openSearch()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // The merged page holds both languages, so a selection can be either side:
  // try the requested direction first, then the opposite one.
  const handleLocateCounterpart = useCallback(async (
    sourceSide: 'original' | 'translated',
    payload: { selectedText: string; page: number; pageCount: number }
  ) => {
    if (!activeId) return
    const fallbackSide = sourceSide === 'original' ? 'translated' : 'original'
    const locate = (side: 'original' | 'translated') => locateCounterpart({
      documentId: activeId,
      source_side: side,
      selected_text: payload.selectedText,
      source_page: payload.page,
      source_page_count: payload.pageCount,
    })
    let located
    try {
      located = await locate(sourceSide)
    } catch {
      try {
        located = await locate(fallbackSide)
      } catch (e: any) {
        alert(`未能定位对应内容：${e?.message ?? String(e)}`)
        return
      }
    }
    await paneRef.current?.locateAndHighlight({
      text: located.target_text,
      highlightText: located.highlight_text,
      positionRatio: located.position_ratio,
    })
  }, [activeId])

  // A library-search hit opens its document and highlights the matched text
  // once the document is available.
  useEffect(() => {
    if (!pendingLocate || !activeDoc || activeDoc.status !== 'done') return
    const { text, side } = pendingLocate
    setPendingLocate(null)
    void handleLocateCounterpart(side, {
      selectedText: text,
      page: 1,
      pageCount: 1,
    })
  }, [pendingLocate, activeDoc, handleLocateCounterpart])

  const handleUpload = useCallback(async (file: File) => {
    setUploading(true)
    try {
      const result = await uploadFile(file)
      setActiveId(result.document_id)
      await refreshSummaries()
    } catch (e: any) {
      if (e?.code === 'config_required' || e?.code === 'worker_unavailable') setSettingsOpen(true)
      setNotice(`上传失败：${e?.message ?? String(e)}`)
    } finally {
      setUploading(false)
    }
  }, [refreshSummaries])

  const handleIncomingFile = useCallback((file: File) => {
    if (!/\.pdf$/i.test(file.name)) {
      setNotice('仅支持 PDF 文件。')
      return
    }
    void handleUpload(file)
  }, [handleUpload])

  const handleToggleFavorite = useCallback((docId: string) => {
    const next = favorites.includes(docId) ? favorites.filter((x) => x !== docId) : [...favorites, docId]
    setFavorites(next)
    void persistPreferences({ favorites: next })
  }, [favorites, persistPreferences])

  const handleDelete = useCallback(async (docId: string) => {
    if (
      !window.confirm(
        '删除这条历史记录？对应的译文 PDF、原文标注 PDF、布局计划等产物，以及上传的原始 PDF，都会从输出目录一并删除。'
      )
    )
      return
    try {
      const result = await deleteDocument(docId)
      setDocCache((prev) => {
        const next = { ...prev }
        delete next[docId]
        return next
      })
      const nextFavorites = favorites.filter((item) => item !== docId)
      setFavorites(nextFavorites)
      void persistPreferences({ favorites: nextFavorites })
      setSummaries((prev) => prev.filter((item) => item.document_id !== docId))
      setActiveId((prev) => (prev === docId ? undefined : prev))
      // Surface a cleanup that could not finish instead of failing silently.
      const problems = (result?.removed ?? []).filter((line) => line.startsWith('could not'))
      if (problems.length) setNotice(`记录已删除，但有文件未能移除：${problems.join('；')}`)
    } catch (e: any) {
      alert(`删除失败：${e?.message ?? String(e)}`)
    }
  }, [favorites, persistPreferences])

  const handleRename = useCallback(async (documentId: string, name: string) => {
    try {
      const updated = await renameDocument(documentId, name)
      setDocCache((cache) => ({ ...cache, [documentId]: updated }))
      await refreshSummaries()
    } catch (e: any) {
      alert(`重命名失败：${e?.message ?? String(e)}`)
    }
  }, [refreshSummaries])

  // Reprocessing from the sidebar targets records that are not necessarily the
  // active one, so keep the list itself fresh while anything is queued.
  const hasQueuedDocuments = summaries.some(
    (item) => item.status === 'queued' || item.status === 'processing'
  )

  useEffect(() => {
    if (!hasQueuedDocuments) return
    const timer = window.setInterval(() => { void refreshSummaries() }, 3000)
    return () => window.clearInterval(timer)
  }, [hasQueuedDocuments, refreshSummaries])

  const stages = activeDoc?.stages ?? []

  const annotatedTitle = useMemo(() => {
    if (!activeDoc) return '原文标注'
    return `原文标注 · ${activeDoc.source_filename || activeDoc.document_id}`
  }, [activeDoc])

  const translatedName = translatedPdfName(activeDoc?.source_filename || 'document.pdf')
  const mergedName = mergedPdfName(activeDoc?.source_filename || 'document.pdf')
  const mergedTitle = `对照 · ${mergedName}`

  return (
    <div className="app-shell">
      {notice && <div className="app-notice" role="alert"><AlertCircle size={16} /><span>{notice}</span><button aria-label="关闭提示" onClick={() => setNotice(null)}>×</button></div>}
      {showSidebar ? (
        <Sidebar
          documents={summaries}
          activeDocumentId={activeId}
          favorites={favorites}
          uploading={uploading}
          theme={theme}
          activeStatus={activeDoc?.status}
          onUpload={handleIncomingFile}
          onSelect={setActiveId}
          onToggleFavorite={handleToggleFavorite}
          onDelete={handleDelete}
          onRename={(id, name) => void handleRename(id, name)}
          onReprocess={(id) => void handleReprocess(id)}
          onCollapse={() => setShowSidebar(false)}
          onOpenSettings={() => setSettingsOpen(true)}
          onToggleTheme={() => {
            const next = theme === 'dark' ? 'light' : 'dark'
            setTheme(next)
            void persistPreferences({ theme: next })
          }}
          onRefreshStatus={refreshActive}
          onSearchLocate={(hit) => {
            if (hit.document_id !== activeId) setActiveId(hit.document_id)
            setPendingLocate({ text: hit.snippet, side: hit.side })
          }}
        />
      ) : (
        <button
          className="sidebar-expand-btn"
          title="打开侧栏"
          onClick={() => setShowSidebar(true)}
        >
          <PanelLeftOpen size={18} />
        </button>
      )}

      <main className="workspace">
        {activeDoc && (
          <ProgressBar
            status={activeDoc.status}
            progress={activeDoc.progress}
            currentStageLabel={activeDoc.current_stage_label}
            etaSeconds={activeDoc.eta_seconds}
            stages={stages}
            failure={activeDoc.failure}
            retrying={retrying}
            onRetry={handleRetry}
            cancelling={cancelling}
            onCancel={handleCancel}
          />
        )}
        {!activeId ? (
          <div className="workspace-empty">
            <div className="empty-illustration"><UploadCloud size={32} /></div>
            <span className="eyebrow">你的本地论文工作台</span>
            <h2>开始阅读</h2>
            <p className="muted">上传 PDF，PaperReader 会保留原文排版并生成可对照阅读的译文。</p>
            <input ref={emptyUploadRef} type="file" accept=".pdf,application/pdf" hidden onChange={(event) => { const file = event.target.files?.[0]; if (file) handleIncomingFile(file); event.currentTarget.value = '' }} />
            <div className="empty-actions"><button className="btn primary" disabled={uploading} onClick={() => emptyUploadRef.current?.click()}>{uploading ? '正在上传…' : '选择 PDF'}</button></div>
            {!settings.api_key_configured && <button className="config-callout" onClick={() => setSettingsOpen(true)}><AlertCircle size={16} />开始前需要配置 AI 服务</button>}
          </div>
        ) : (
          <PdfPane
            ref={paneRef}
            title={showAnnotated ? annotatedTitle : mergedTitle}
            pdfUrl={panePdfUrl}
            downloads={[
              { title: '下载译文 PDF', label: '译文', href: translatedPdfUrl, name: translatedName },
              { title: '下载双语对照 PDF', label: '双语', href: mergedPdfUrl, name: mergedName }
            ]}
            counterpartLabel="对应内容"
            onLocateCounterpart={(payload) => void handleLocateCounterpart('original', payload)}
            annotations={annotations}
            onCreateAnnotation={handleCreateAnnotation}
            onDeleteAnnotation={handleDeleteAnnotation}
            onExportNotes={handleExportNotes}
            initialPosition={activeDoc ? { page: activeDoc.last_read_page, ratio: activeDoc.last_read_ratio } : null}
            onProgressChange={handleProgressChange}
            figures={structureFigures}
            outline={structureOutline}
          />
        )}
      </main>

      <SettingsModal
        open={settingsOpen}
        settings={settings}
        onClose={() => setSettingsOpen(false)}
        onSettingsChange={onSettingsChange}
      />
    </div>
  )
}
