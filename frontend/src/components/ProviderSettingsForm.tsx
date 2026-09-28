import { useState } from 'react'
import { CheckCircle2, CircleAlert, KeyRound } from 'lucide-react'
import {
  ApiError,
  PROVIDER_PRESETS,
  testProviderConnection,
  type ProviderSettingsDraft,
  type ProviderTestResult
} from '../lib/api'

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
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<ProviderTestResult | null>(null)

  const set = <K extends keyof ProviderSettingsDraft>(key: K, next: ProviderSettingsDraft[K]) => {
    setTestResult(null)
    onChange({ ...value, [key]: next })
  }

  const preset = PROVIDER_PRESETS.find((item) => item.id === value.provider)

  const selectProvider = (id: string) => {
    const next = PROVIDER_PRESETS.find((item) => item.id === id)
    setTestResult(null)
    if (next) {
      onChange({ ...value, provider: id, base_url: next.base_url, model: next.model })
    } else {
      onChange({ ...value, provider: 'custom' })
    }
  }

  // Test the draft exactly as shown, so unsaved edits are covered too; the
  // masked key means "use the stored one", which the backend falls back to.
  const runTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const result = await testProviderConnection({
        api_key: !value.api_key || value.api_key === MASKED_API_KEY ? undefined : value.api_key,
        base_url: value.base_url || undefined,
        model: value.model || undefined
      })
      setTestResult(result)
    } catch (error) {
      setTestResult({
        ok: false,
        message: error instanceof ApiError ? error.message : '测试失败，请稍后重试。'
      })
    } finally {
      setTesting(false)
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
        <label className="field">
          <span>思考</span>
          <select
            value={value.enable_thinking ? 'on' : 'off'}
            onChange={(e) => set('enable_thinking', e.target.value === 'on')}
          >
            <option value="off">关闭</option>
            <option value="on">开启</option>
          </select>
        </label>
      </div>

      <div className="provider-test">
        <button type="button" className="btn" onClick={() => void runTest()} disabled={testing}>
          {testing ? '测试中…' : '测试连接'}
        </button>
        {testResult && (
          <span className={testResult.ok ? 'form-success' : 'form-error'} role="status">
            {testResult.message}
          </span>
        )}
      </div>
    </div>
  )
}
