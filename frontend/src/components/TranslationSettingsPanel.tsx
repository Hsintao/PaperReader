import { useCallback, useEffect, useState } from 'react'
import { Languages, RefreshCw, Trash2 } from 'lucide-react'
import {
  TRANSLATION_DOMAINS,
  deleteGlossaryTerm,
  getGlossary,
  refreshGlossary,
  type GlossarySnapshot,
  type TranslationDomain
} from '../lib/api'

type Props = {
  domain: TranslationDomain
  onDomainChange: (domain: TranslationDomain) => void
}

function formatUpdatedAt(value?: string | null): string {
  if (!value) return '尚未合并'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '尚未合并'
  return parsed.toLocaleString()
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

  async function removeTerm(en: string) {
    setBusy(true)
    setError(null)
    try {
      setSnapshot(await deleteGlossaryTerm(domain, en))
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
        术语 {snapshot?.term_count ?? 0} 条 · 待合并 {snapshot?.pending_count ?? 0} 条 · 最近更新 {formatUpdatedAt(snapshot?.updated_at)}
      </p>

      {snapshot && snapshot.terms.length > 0 ? (
        <div className="glossary-list">
          {snapshot.terms.map((term) => (
            <div className="glossary-term" key={term.en}>
              <span className="glossary-term-source">{term.en}</span>
              <span className="glossary-term-target">{term.zh}</span>
              <span className="glossary-term-count">{term.count}</span>
              <button
                className="icon-btn"
                aria-label={`删除术语 ${term.en}`}
                onClick={() => void removeTerm(term.en)}
                disabled={busy}
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      ) : (
        <p className="glossary-empty">术语库会从已翻译的内容中自动积累，并按设定间隔合并。</p>
      )}

      {error && <div className="form-error" role="status">{error}</div>}
    </section>
  )
}
