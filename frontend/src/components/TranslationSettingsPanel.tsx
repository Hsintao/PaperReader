import { useCallback, useEffect, useState } from 'react'
import { Languages, RefreshCw } from 'lucide-react'
import {
  TRANSLATION_DOMAINS,
  getGlossary,
  refreshGlossary,
  type GlossarySnapshot,
  type TranslationDomain
} from '../lib/api'

type Props = {
  domain: TranslationDomain
  onDomainChange: (domain: TranslationDomain) => void
}

function formatBytes(bytes: number): string {
  if (!bytes) return '0 KB'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

export function TranslationSettingsPanel({ domain, onDomainChange }: Props) {
  const [snapshot, setSnapshot] = useState<GlossarySnapshot | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const active = TRANSLATION_DOMAINS.find((item) => item.id === domain) || TRANSLATION_DOMAINS[2]

  const load = useCallback(async (target: TranslationDomain) => {
    setBusy(true)
    setError(null)
    try {
      setSnapshot(await getGlossary(target))
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => { void load(domain) }, [domain, load])

  async function refreshNow() {
    setBusy(true)
    setError(null)
    try {
      setSnapshot(await refreshGlossary(domain))
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="profile-section borderless">
      <div className="settings-section-heading">
        <div>
          <h3>翻译领域</h3>
          <p>领域决定翻译提示词中的术语规范，并对应一套独立的术语库。保存后对新的翻译任务生效。</p>
        </div>
        <span className="config-status ready"><Languages size={14} />{active.label}</span>
      </div>

      <div className="segmented-control three" role="radiogroup" aria-label="翻译领域">
        {TRANSLATION_DOMAINS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="radio"
            aria-checked={item.id === domain}
            className={item.id === domain ? 'active' : ''}
            onClick={() => onDomainChange(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <p className="glossary-meta">{active.hint}</p>

      <div className="settings-divider" />
      <div className="settings-section-heading">
        <div>
          <h3>术语库</h3>
          <p>术语从已翻译的内容中自动积累，每 {snapshot?.interval_minutes ?? 30} 分钟合并一次；对应英文出现时按此译法强制翻译。</p>
        </div>
        <button className="btn" onClick={() => void refreshNow()} disabled={busy}>
          <RefreshCw size={14} />{busy ? '处理中…' : '立即更新'}
        </button>
      </div>

      <p className="glossary-meta">
        当前大小 {formatBytes(snapshot?.size_bytes ?? 0)}
      </p>

      {error && <div className="form-error" role="status">{error}</div>}
    </section>
  )
}
