import { CheckCircle2, CircleAlert, KeyRound } from 'lucide-react'
import type { ProviderSettingsDraft } from '../lib/api'

type Props = {
  value: ProviderSettingsDraft
  onChange: (value: ProviderSettingsDraft) => void
  apiKeyConfigured?: boolean
  somarkKeyConfigured?: boolean
  mineruKeyConfigured?: boolean
  allowClear?: boolean
  showHeading?: boolean
}

export function ProviderSettingsForm({
  value,
  onChange,
  apiKeyConfigured = false,
  somarkKeyConfigured = false,
  mineruKeyConfigured = false,
  allowClear = false,
  showHeading = true
}: Props) {
  const set = <K extends keyof ProviderSettingsDraft>(key: K, next: ProviderSettingsDraft[K]) => {
    onChange({ ...value, [key]: next })
  }

  const parserKeyConfigured =
    value.pdf_parser === 'somark' ? somarkKeyConfigured
    : value.pdf_parser === 'mineru' ? mineruKeyConfigured
    : true
  const parserStatusLabel =
    value.pdf_parser === 'local' ? '本地'
    : parserKeyConfigured ? (value.pdf_parser === 'somark' ? 'SoMark 已配置' : 'MinerU 已配置')
    : '需要密钥'

  return (
    <div className="provider-form">
      {showHeading && (
        <div className="settings-section-heading">
          <div>
            <h3>大模型服务</h3>
            <p>用于论文翻译、问答和视觉校验。密钥只加密保存在本机。</p>
          </div>
          <span className={`config-status ${apiKeyConfigured ? 'ready' : 'missing'}`}>
            {apiKeyConfigured ? <CheckCircle2 size={14} /> : <CircleAlert size={14} />}
            {apiKeyConfigured ? '已配置' : '待配置'}
          </span>
        </div>
      )}

      <div className="field-grid two">
        <label className="field field-span-2">
          <span>API Key {apiKeyConfigured && '（留空则保持不变）'}</span>
          <div className="secret-field">
            <KeyRound size={15} />
            <input
              type="password"
              autoComplete="off"
              value={value.api_key}
              onChange={(e) => set('api_key', e.target.value)}
              placeholder={apiKeyConfigured ? '••••••••••••••••' : '输入你的 API Key'}
            />
          </div>
        </label>
        <label className="field">
          <span>Base URL</span>
          <input value={value.base_url} onChange={(e) => set('base_url', e.target.value)} />
        </label>
        <label className="field">
          <span>模型名称</span>
          <input value={value.model} onChange={(e) => set('model', e.target.value)} />
        </label>
      </div>
      {allowClear && apiKeyConfigured && (
        <label className="check-row danger-check">
          <input
            type="checkbox"
            checked={!!value.clear_api_key}
            onChange={(e) => set('clear_api_key', e.target.checked)}
          />
          <span>删除已保存的大模型 API Key</span>
        </label>
      )}

      <div className="settings-divider" />
      <div className="settings-section-heading">
        <div>
          <h3>PDF 解析</h3>
          <p>默认使用 SoMark 云解析公式、图表和复杂排版，也可切换到 MinerU 或无需密钥的本地解析。</p>
        </div>
        <span className={`config-status ${parserKeyConfigured ? 'ready' : 'missing'}`}>
          {parserKeyConfigured ? <CheckCircle2 size={14} /> : <CircleAlert size={14} />}
          {parserStatusLabel}
        </span>
      </div>

      <div className="segmented-control three" role="radiogroup" aria-label="PDF 解析方式">
        <button
          type="button"
          role="radio"
          aria-checked={value.pdf_parser === 'somark'}
          className={value.pdf_parser === 'somark' ? 'active' : ''}
          onClick={() => set('pdf_parser', 'somark')}
        >
          SoMark 云解析
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={value.pdf_parser === 'local'}
          className={value.pdf_parser === 'local' ? 'active' : ''}
          onClick={() => set('pdf_parser', 'local')}
        >
          本地解析
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={value.pdf_parser === 'mineru'}
          className={value.pdf_parser === 'mineru' ? 'active' : ''}
          onClick={() => set('pdf_parser', 'mineru')}
        >
          MinerU 云解析
        </button>
      </div>

      {value.pdf_parser === 'somark' && (
        <div className="field-grid two parser-fields">
          <label className="field field-span-2">
            <span>SoMark API Key {somarkKeyConfigured && '（留空则保持不变）'}</span>
            <div className="secret-field">
              <KeyRound size={15} />
              <input
                type="password"
                autoComplete="off"
                value={value.somark_api_key}
                onChange={(e) => set('somark_api_key', e.target.value)}
                placeholder={somarkKeyConfigured ? '••••••••••••••••' : '在 somark.cn 控制台 API Workbench → APIKey 获取'}
              />
            </div>
          </label>
          <label className="field field-span-2">
            <span>SoMark Base URL</span>
            <input
              value={value.somark_base_url}
              onChange={(e) => set('somark_base_url', e.target.value)}
              placeholder="https://somark.cn/api/v1"
            />
            <small className="muted small">中国大陆用 https://somark.cn/api/v1，海外用 https://somark.ai/api/v1</small>
          </label>
          {allowClear && somarkKeyConfigured && (
            <label className="check-row danger-check"><input type="checkbox" checked={!!value.clear_somark_api_key} onChange={(e) => set('clear_somark_api_key', e.target.checked)} /><span>删除已保存的 SoMark Key</span></label>
          )}
        </div>
      )}

      {value.pdf_parser === 'mineru' && (
        <div className="field-grid two parser-fields">
          <label className="field field-span-2">
            <span>MinerU API Key {mineruKeyConfigured && '（留空则保持不变）'}</span>
            <div className="secret-field">
              <KeyRound size={15} />
              <input
                type="password"
                autoComplete="off"
                value={value.mineru_api_key}
                onChange={(e) => set('mineru_api_key', e.target.value)}
                placeholder={mineruKeyConfigured ? '••••••••••••••••' : '输入 MinerU API Key'}
              />
            </div>
          </label>
          <label className="field field-span-2">
            <span>MinerU Base URL</span>
            <input value={value.mineru_base_url} onChange={(e) => set('mineru_base_url', e.target.value)} />
          </label>
          <label className="field"><span>模型版本</span><input value={value.mineru_model_version} onChange={(e) => set('mineru_model_version', e.target.value)} /></label>
          <label className="field"><span>文档语言</span><input value={value.mineru_language} onChange={(e) => set('mineru_language', e.target.value)} /></label>
          <label className="check-row"><input type="checkbox" checked={value.mineru_enable_formula} onChange={(e) => set('mineru_enable_formula', e.target.checked)} /><span>识别公式</span></label>
          <label className="check-row"><input type="checkbox" checked={value.mineru_enable_table} onChange={(e) => set('mineru_enable_table', e.target.checked)} /><span>识别表格</span></label>
          <label className="check-row"><input type="checkbox" checked={value.mineru_is_ocr} onChange={(e) => set('mineru_is_ocr', e.target.checked)} /><span>强制 OCR</span></label>
          {allowClear && mineruKeyConfigured && (
            <label className="check-row danger-check"><input type="checkbox" checked={!!value.clear_mineru_api_key} onChange={(e) => set('clear_mineru_api_key', e.target.checked)} /><span>删除已保存的 MinerU Key</span></label>
          )}
        </div>
      )}

      <label className="field">
        <span>视觉校验模型</span>
        <input value={value.vision_model} onChange={(e) => set('vision_model', e.target.value)} />
      </label>
    </div>
  )
}
