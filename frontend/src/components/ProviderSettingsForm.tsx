import { CheckCircle2, CircleAlert, KeyRound } from 'lucide-react'
import { PROVIDER_PRESETS, type ProviderSettingsDraft } from '../lib/api'

// Stands in for the stored key while a key is configured: saving with the
// mask untouched keeps the key, saving with the field emptied deletes it.
export const MASKED_API_KEY = '••••••••••••••••'

type Props = {
  value: ProviderSettingsDraft
  onChange: (value: ProviderSettingsDraft) => void
  apiKeyConfigured?: boolean
}

export function ProviderSettingsForm({
  value,
  onChange,
  apiKeyConfigured = false
}: Props) {
  const set = <K extends keyof ProviderSettingsDraft>(key: K, next: ProviderSettingsDraft[K]) => {
    onChange({ ...value, [key]: next })
  }

  const preset = PROVIDER_PRESETS.find((item) => item.id === value.provider)

  const selectProvider = (id: string) => {
    const next = PROVIDER_PRESETS.find((item) => item.id === id)
    if (next) {
      onChange({ ...value, provider: id, base_url: next.base_url, model: next.model })
    } else {
      set('provider', 'custom')
    }
  }

  return (
    <div className="provider-form">
      <div className="settings-section-heading">
        <div>
          <h3>模型设置</h3>
          <p>用于论文翻译与问答。密钥只加密保存在本机。</p>
        </div>
        <span className={`config-status ${apiKeyConfigured ? 'ready' : 'missing'}`}>
          {apiKeyConfigured ? <CheckCircle2 size={14} /> : <CircleAlert size={14} />}
          {apiKeyConfigured ? '已配置' : '待配置'}
        </span>
      </div>

      <div className="field-grid two">
        <label className="field field-span-2">
          <span>服务提供商</span>
          <select value={preset ? preset.id : 'custom'} onChange={(e) => selectProvider(e.target.value)}>
            {PROVIDER_PRESETS.map((item) => (
              <option key={item.id} value={item.id}>{item.label}</option>
            ))}
            <option value="custom">自定义（OpenAI 兼容接口）</option>
          </select>
        </label>
        <label className="field field-span-2">
          <span>API Key {apiKeyConfigured && '（留空将删除已保存的 Key）'}</span>
          <div className="secret-field">
            <KeyRound size={15} />
            <input
              type="password"
              autoComplete="off"
              value={value.api_key}
              onChange={(e) => set('api_key', e.target.value)}
              onFocus={(e) => {
                if (value.api_key === MASKED_API_KEY) e.currentTarget.select()
              }}
              placeholder={apiKeyConfigured ? '留空将删除已保存的 Key' : `输入你的 API Key${preset ? `（${preset.key_hint}）` : ''}`}
            />
          </div>
        </label>
        <label className="field">
          <span>Base URL</span>
          <input value={value.base_url} onChange={(e) => set('base_url', e.target.value)} />
        </label>
        <label className="field">
          <span>模型名称</span>
          <input
            value={value.model}
            onChange={(e) => set('model', e.target.value)}
            list={preset ? `provider-models-${preset.id}` : undefined}
          />
          {preset && (
            <datalist id={`provider-models-${preset.id}`}>
              {preset.models.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          )}
        </label>
      </div>
    </div>
  )
}
