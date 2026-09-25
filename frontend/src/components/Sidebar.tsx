import { useEffect, useRef, useState } from 'react'
import {
  ChevronRight,
  Cloud,
  FileText,
  FolderOpen,
  Moon,
  PanelLeftClose,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Star,
  Sun,
  Trash2,
} from 'lucide-react'
import type {
  DocumentSummary,
  LibrarySearchHit,
  Theme,
} from '../lib/api'
import { getDocumentBibtex, searchLibrary } from '../lib/api'
import { THEME_CYCLE, THEME_LABELS } from '../lib/theme'

type Tab = 'tasks' | 'favorites'

const THEME_ICONS = {
  light: Sun,
  gray: Cloud,
  dark: Moon,
} as const

type Props = {
  documents: DocumentSummary[]
  activeDocumentId?: string
  favorites: string[]
  uploading: boolean
  theme: Theme
  activeStatus?: string
  onUpload: (file: File) => void
  onSelect: (documentId: string) => void
  onToggleFavorite: (documentId: string) => void
  onDelete: (documentId: string) => void
  onRename: (documentId: string, name: string) => void
  onReprocess: (documentId: string) => void
  onCollapse: () => void
  onOpenSettings: () => void
  onToggleTheme: () => void
  onRefreshStatus: () => void
  onSearchLocate: (hit: LibrarySearchHit) => void
}

function formatSize(bytes: number): string {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function StatusBadge({ status }: { status: string }) {
  const cls = status === 'done' ? 'badge done' : status === 'failed' ? 'badge failed' : 'badge pending'
  return <span className={cls}>{status}</span>
}

function formatTime(value?: string | null): string {
  if (!value) return '刚刚创建'
  return new Date(value).toLocaleString()
}

export function Sidebar({
  documents,
  activeDocumentId,
  favorites,
  uploading,
  theme,
  activeStatus,
  onUpload,
  onSelect,
  onToggleFavorite,
  onDelete,
  onRename,
  onReprocess,
  onCollapse,
  onOpenSettings,
  onToggleTheme,
  onRefreshStatus,
  onSearchLocate,
}: Props) {
  const ThemeIcon = THEME_ICONS[theme]
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [tab, setTab] = useState<Tab>('tasks')
  const [contextMenu, setContextMenu] = useState<{ documentId: string; x: number; y: number } | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchHits, setSearchHits] = useState<LibrarySearchHit[]>([])
  const [searching, setSearching] = useState(false)

  // Full-text library search with a light debounce; empty query clears.
  useEffect(() => {
    const query = searchQuery.trim()
    if (!query) {
      setSearchHits([])
      setSearching(false)
      return
    }
    setSearching(true)
    const timer = window.setTimeout(() => {
      void searchLibrary(query)
        .then((hits) => setSearchHits(hits))
        .catch(() => setSearchHits([]))
        .finally(() => setSearching(false))
    }, 300)
    return () => window.clearTimeout(timer)
  }, [searchQuery])

  async function exportBibtex(documentId: string) {
    try {
      const { bibtex, filename } = await getDocumentBibtex(documentId)
      const blob = new Blob([bibtex], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      link.click()
      URL.revokeObjectURL(url)
    } catch (e: any) {
      alert(`导出 BibTeX 失败：${e?.message ?? String(e)}`)
    }
  }

  const visible =
    tab === 'favorites'
      ? documents.filter((d) => favorites.includes(d.document_id))
      : documents

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="brand">
          <span className="brand-dot" />
          PaperReader
        </div>
        <button className="icon-btn" title="收起侧栏" onClick={onCollapse}>
          <PanelLeftClose size={18} />
        </button>
      </div>

      <div className="sidebar-toolbar" role="toolbar" aria-label="工具">
        <button
          className="icon-btn"
          title={`刷新状态：${activeStatus ?? '—'}`}
          onClick={onRefreshStatus}
        >
          <RefreshCw size={16} />
        </button>
        <div className="toolbar-spacer" />
        <button
          className="icon-btn"
          title={`切换到${THEME_LABELS[THEME_CYCLE[theme]]}`}
          onClick={onToggleTheme}
        >
          <ThemeIcon size={16} />
        </button>
        <button className="icon-btn" title="设置" onClick={onOpenSettings}>
          <Settings size={16} />
        </button>
      </div>

      <button
        className="new-parse-btn"
        disabled={uploading}
        onClick={() => fileInputRef.current?.click()}
      >
        <Plus size={16} />
        {uploading ? '上传中…' : '新解析'}
      </button>
      <input
        ref={fileInputRef}
        type="file"
        accept=".pdf,application/pdf"
        style={{ display: 'none' }}
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (!file) return
          onUpload(file)
          e.currentTarget.value = ''
        }}
      />

      <nav className="sidebar-nav">
        <button
          className={`nav-item ${tab === 'tasks' ? 'active' : ''}`}
          onClick={() => setTab('tasks')}
        >
          <FolderOpen size={16} />
          历史记录
        </button>
        <button
          className={`nav-item ${tab === 'favorites' ? 'active' : ''}`}
          onClick={() => setTab('favorites')}
        >
          <Star size={16} />
          我的收藏
        </button>
      </nav>

      <div className="library-search">
        <Search size={14} />
        <input
          type="text"
          placeholder="全文搜索文献库…"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
        />
        {searchQuery && (
          <button className="icon-btn" title="清除搜索" onClick={() => setSearchQuery('')}>
            <Trash2 size={12} />
          </button>
        )}
      </div>

      <div className="sidebar-scroll">
        <div className="sidebar-divider" />

        {searchQuery.trim() ? (
          <div className="doc-list">
            {searching ? (
              <div className="muted small" style={{ padding: 12 }}>搜索中…</div>
            ) : searchHits.length === 0 ? (
              <div className="muted small" style={{ padding: 12 }}>没有匹配的文本</div>
            ) : (
              searchHits.map((hit) => (
                <button
                  key={`${hit.document_id}-${hit.side}`}
                  className={`doc-item search-hit ${hit.document_id === activeDocumentId ? 'active' : ''}`}
                  onClick={() => onSearchLocate(hit)}
                  title={hit.snippet}
                >
                  <div className="doc-icon"><FileText size={18} /></div>
                  <div className="doc-meta">
                    <div className="doc-name">{hit.document_title}</div>
                    <div className="doc-sub">
                      <span className="muted small">{hit.side === 'original' ? '原文' : '译文'}</span>
                    </div>
                    <div className="muted tiny">{hit.snippet.slice(0, 60)}…</div>
                  </div>
                </button>
              ))
            )}
          </div>
        ) : (
        <div className="doc-list">
        {visible.length === 0 ? (
          <div className="muted small" style={{ padding: '12px' }}>
            {tab === 'favorites' ? '尚无收藏' : '暂无历史记录，点击「新解析」上传 PDF'}
          </div>
        ) : (
          visible.map((doc) => {
            const active = doc.document_id === activeDocumentId
            const fav = favorites.includes(doc.document_id)
            const busy = doc.status === 'queued' || doc.status === 'processing'
            const displayName = doc.title || doc.source_filename || doc.document_id
            return (
              <div
                key={doc.document_id}
                className={`doc-item ${active ? 'active' : ''}`}
                onClick={() => onSelect(doc.document_id)}
                onContextMenu={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  setContextMenu({ documentId: doc.document_id, x: e.clientX, y: e.clientY })
                }}
              >
                <div className="doc-icon">
                  <FileText size={20} />
                </div>
                <div className="doc-meta">
                  <div className="doc-name" title={doc.source_filename}>{displayName}</div>
                  <div className="doc-sub">
                    <span>{doc.year || formatSize(doc.size_bytes)}</span>
                    <StatusBadge status={doc.status} />
                  </div>
                  <div className="muted tiny">{formatTime(doc.last_opened_at || doc.updated_at || doc.created_at)}</div>
                </div>
                <div className="doc-actions">
                  <button
                    className={`star-btn ${fav ? 'on' : ''}`}
                    title={fav ? '取消收藏' : '收藏'}
                    onClick={(e) => {
                      e.stopPropagation()
                      onToggleFavorite(doc.document_id)
                    }}
                  >
                    <Star size={14} fill={fav ? 'currentColor' : 'none'} />
                  </button>
                  <button
                    className="star-btn"
                    title={busy ? '处理中，无法重新处理' : '重新处理（重新解析并翻译）'}
                    disabled={busy}
                    onClick={(e) => {
                      e.stopPropagation()
                      onReprocess(doc.document_id)
                    }}
                  >
                    <RefreshCw size={14} />
                  </button>
                  <button
                    className="star-btn"
                    title="移除历史"
                    onClick={(e) => {
                      e.stopPropagation()
                      onDelete(doc.document_id)
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
                <ChevronRight size={14} className="chev" />
              </div>
            )
          })
        )}
      </div>
        )}
      </div>

      {contextMenu && (
        <>
          <div
            className="context-menu-overlay"
            onClick={() => setContextMenu(null)}
            onContextMenu={(e) => {
              e.preventDefault()
              setContextMenu(null)
            }}
          />
          <div className="context-menu" style={{ top: contextMenu.y, left: contextMenu.x }}>
            <button
              className="context-menu-item"
              onClick={() => {
                const id = contextMenu.documentId
                const current = documents.find((item) => item.document_id === id)?.source_filename || ''
                setContextMenu(null)
                const next = window.prompt('请输入新的文档名', current)
                if (next && next.trim() && next.trim() !== current) {
                  onRename(id, next.trim())
                }
              }}
            >
              <Pencil size={14} />
              更改文档名
            </button>
            <button
              className="context-menu-item"
              onClick={() => {
                const id = contextMenu.documentId
                setContextMenu(null)
                void exportBibtex(id)
              }}
            >
              <FileText size={14} />
              导出 BibTeX
            </button>
            <button
              className="context-menu-item danger"
              onClick={() => {
                const id = contextMenu.documentId
                setContextMenu(null)
                onDelete(id)
              }}
            >
              <Trash2 size={14} />
              删除文档
            </button>
          </div>
        </>
      )}
    </aside>
  )
}
