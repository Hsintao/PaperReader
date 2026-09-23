import { CheckCircle2, CircleAlert, KeyRound } from 'lucide-react'
import type { ProviderSettingsDraft } from '../lib/api'

type Props = {
  value: ProviderSettingsDraft
  onChange: (value: ProviderSettingsDraft) => void
  apiKeyConfigured?: boolean
  allowClear?: boolean
}

export function ProviderSettingsForm({
  value,
  onChange,
  apiKeyConfigured = false,
  allowClear = false
}: Props) {
  const set = <K extends keyof ProviderSettingsDraft>(key: K, next: ProviderSettingsDraft[K]) => {
    onChange({ ...value, [key]: next })
  }

  return (
    <div className="provider-form">
      <div className="settings-section-heading">
        <div>
          <h3>大模型服务</h3>
          <p>用于论文翻译与问答。密钥只加密保存在本机。</p>
        </div>
        <span className={`config-status ${apiKeyConfigured ? 'ready' : 'missing'}`}>
          {apiKeyConfigured ? <CheckCircle2 size={14} /> : <CircleAlert size={14} />}
          {apiKeyConfigured ? '已配置' : '待配置'}
        </span>
      </div>

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
    </div>
  )
}
