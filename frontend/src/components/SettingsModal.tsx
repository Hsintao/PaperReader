import { useEffect, useRef, useState } from 'react'
import { Bot, BookOpen, Languages, X } from 'lucide-react'
import {
  updateProviderSettings,
  updateSettings,
  type ProviderSettingsDraft,
  type TranslationDomain,
  type UserSettings
} from '../lib/api'
import { ProviderSettingsForm } from './ProviderSettingsForm'
import { TranslationSettingsPanel } from './TranslationSettingsPanel'

type Props = {
  open: boolean
  settings: UserSettings
  onClose: () => void
  onSettingsChange: (settings: UserSettings) => void
}

type Tab = 'providers' | 'translation' | 'reading'

function providerDraft(settings: UserSettings): ProviderSettingsDraft {
  return {
    api_key: '', base_url: settings.base_url, model: settings.model,
    pdf_parser: settings.pdf_parser, somark_api_key: '',
    somark_base_url: settings.somark_base_url, mineru_api_key: '',
    mineru_base_url: settings.mineru_base_url,
    mineru_model_version: settings.mineru_model_version,
    mineru_language: settings.mineru_language,
    mineru_enable_formula: settings.mineru_enable_formula,
    mineru_enable_table: settings.mineru_enable_table,
    mineru_is_ocr: settings.mineru_is_ocr,
    vision_model: settings.vision_model
  }
}

export function SettingsModal({ open, settings, onClose, onSettingsChange }: Props) {
  const [tab, setTab] = useState<Tab>('providers')
  const [reading, setReading] = useState<UserSettings>(settings)
  const [domain, setDomain] = useState<TranslationDomain>(settings.translation_domain)
  const [providers, setProviders] = useState<ProviderSettingsDraft>(() => providerDraft(settings))
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const closeButtonRef = useRef<HTMLButtonElement | null>(null)

  // Seed the working copies when the dialog opens. Saving updates `settings`
  // too, so depending on it here would reset the active tab and drop the
  // confirmation right after a save.
  useEffect(() => {
    if (!open) return
    setTab(settings.api_key_configured ? 'reading' : 'providers')
    setReading(settings)
    setDomain(settings.translation_domain)
    setProviders(providerDraft(settings))
    setMessage(null)
    setError(null)
    window.setTimeout(() => closeButtonRef.current?.focus(), 0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => {
    if (!open) return
    const closeOnEscape = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [open, onClose])

  if (!open) return null

  async function saveCurrentTab() {
    setBusy(true); setError(null); setMessage(null)
    try {
      let next = settings
      if (tab === 'providers') {
        next = await updateProviderSettings(providers)
        setProviders(providerDraft(next))
      } else if (tab === 'translation') {
        next = await updateSettings({ translation_domain: domain })
        setDomain(next.translation_domain)
      } else {
        next = await updateSettings({
          theme: reading.theme,
          vision_enabled: reading.vision_enabled,
          vision_mode: reading.vision_mode,
          show_annotated_pdf: reading.show_annotated_pdf
        })
      }
      onSettingsChange(next)
      setMessage('设置已保存。')
    } catch (e: any) { setError(e?.message || String(e)) } finally { setBusy(false) }
  }

  return (
    <div className="modal-overlay" onMouseDown={onClose}>
      <div className="modal-card profile-card" role="dialog" aria-modal="true" aria-labelledby="settings-title" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div><div className="eyebrow">本机配置</div><div className="modal-title" id="settings-title">设置</div></div>
          <button ref={closeButtonRef} className="icon-btn" aria-label="关闭设置" onClick={onClose}><X size={17} /></button>
        </div>
        <div className="profile-layout">
          <nav className="profile-tabs" aria-label="设置导航">
            <button className={tab === 'providers' ? 'active' : ''} onClick={() => setTab('providers')}><Bot size={16} />AI 服务{!settings.api_key_configured && <span className="attention-dot" />}</button>
            <button className={tab === 'translation' ? 'active' : ''} onClick={() => setTab('translation')}><Languages size={16} />翻译设置</button>
            <button className={tab === 'reading' ? 'active' : ''} onClick={() => setTab('reading')}><BookOpen size={16} />阅读偏好</button>
          </nav>
          <div className="modal-body profile-body">
            {tab === 'providers' && <ProviderSettingsForm value={providers} onChange={setProviders} apiKeyConfigured={settings.api_key_configured} somarkKeyConfigured={settings.somark_api_key_configured} mineruKeyConfigured={settings.mineru_api_key_configured} allowClear />}
            {tab === 'translation' && <TranslationSettingsPanel domain={domain} onDomainChange={setDomain} />}
            {tab === 'reading' && (
              <section className="profile-section borderless">
                <div className="settings-section-heading"><div><h3>阅读体验</h3><p>这些偏好保存在本机数据目录。</p></div></div>
                <div className="field-grid two">
                  <label className="field"><span>主题</span><select value={reading.theme} onChange={(e) => setReading((v) => ({ ...v, theme: e.target.value as 'light' | 'dark' }))}><option value="light">浅色</option><option value="dark">深色</option></select></label>
                  <label className="field"><span>视觉校验</span><select value={reading.vision_enabled ? reading.vision_mode : 'off'} onChange={(e) => { const next = e.target.value; setReading((v) => ({ ...v, vision_enabled: next !== 'off', vision_mode: next === 'manual' ? 'manual' : 'auto' })) }}><option value="auto">开启 · 自动</option><option value="manual">开启 · 人工</option><option value="off">关闭</option></select></label>
                  <label className="field"><span>原文标注</span><select value={reading.show_annotated_pdf ? 'on' : 'off'} onChange={(e) => setReading((v) => ({ ...v, show_annotated_pdf: e.target.value === 'on' }))}><option value="off">关闭 · 左侧显示原始 PDF</option><option value="on">开启 · 左侧显示标注版本</option></select></label>
                </div>
              </section>
            )}
            {(message || error) && <div className={error ? 'form-error' : 'form-success'} role="status">{error || message}</div>}
          </div>
        </div>
        <div className="modal-footer"><button className="btn" onClick={onClose} disabled={busy}>关闭</button><button className="btn primary" onClick={() => void saveCurrentTab()} disabled={busy}>{busy ? '保存中…' : '保存当前页'}</button></div>
      </div>
    </div>
  )
}
