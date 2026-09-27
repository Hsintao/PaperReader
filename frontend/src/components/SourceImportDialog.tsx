import { useEffect, useRef, useState } from 'react'
import { FileUp, X } from 'lucide-react'

type Props = {
  open: boolean
  busy: boolean
  onClose: () => void
  onLocalFile: (file: File) => void
  onImport: (source: string) => void
}

export function SourceImportDialog({ open, busy, onClose, onLocalFile, onImport }: Props) {
  const [source, setSource] = useState('')
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const closeButtonRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    if (!open) return
    setSource('')
    window.setTimeout(() => closeButtonRef.current?.focus(), 0)
  }, [open])

  useEffect(() => {
    if (!open) return
    const closeOnEscape = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [open, onClose])

  if (!open) return null

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    const value = source.trim()
    if (!value || busy) return
    onImport(value)
  }

  return (
    <div className="modal-overlay" onMouseDown={onClose}>
      <div
        className="modal-card source-import-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="source-import-title"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <div><div className="eyebrow">新解析</div><div className="modal-title" id="source-import-title">导入 PDF</div></div>
          <button ref={closeButtonRef} className="icon-btn" aria-label="关闭导入" onClick={onClose}><X size={17} /></button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="modal-body">
            <button
              type="button"
              className="btn source-import-local"
              disabled={busy}
              onClick={() => fileInputRef.current?.click()}
            >
              <FileUp size={16} /> 选择本地 PDF
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,application/pdf"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) onLocalFile(file)
                event.currentTarget.value = ''
              }}
            />
            <div className="source-import-divider"><span>或输入路径 / 链接</span></div>
            <label className="field">
              <span>PDF 路径或链接</span>
              <input
                autoFocus
                type="text"
                placeholder="例如 /Users/me/papers/example.pdf、https://example.org/paper.pdf 或 https://arxiv.org/abs/2401.00001"
                value={source}
                onChange={(e) => setSource(e.target.value)}
                disabled={busy}
              />
            </label>
            <p className="muted small">支持本机路径、http(s) PDF 链接和 arXiv 链接。</p>
          </div>
          <div className="modal-footer">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
            <button type="submit" className="btn primary" disabled={busy || !source.trim()}>{busy ? '导入中…' : '导入'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}
