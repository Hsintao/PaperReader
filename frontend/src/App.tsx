import { lazy, Suspense, useEffect, useState } from 'react'
import { getSettings, type UserSettings } from './lib/api'

const ReaderPage = lazy(() => import('./pages/ReaderPage').then((module) => ({ default: module.ReaderPage })))

export function App() {
  const [settings, setSettings] = useState<UserSettings | null>(null)
  const [startupError, setStartupError] = useState<string | null>(null)

  async function initialize() {
    setStartupError(null)
    try {
      setSettings(await getSettings())
    } catch (error: any) {
      setStartupError(error?.message || '无法连接 PaperReader 服务。')
    }
  }

  useEffect(() => {
    void initialize()
    // initialize is intentionally run once at startup.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (startupError) {
    return (
      <div className="auth-shell">
        <div className="auth-card compact">
          <h2>服务暂时不可用</h2>
          <p className="muted">{startupError}</p>
          <button className="btn primary" onClick={() => void initialize()}>重新连接</button>
        </div>
      </div>
    )
  }

  if (!settings) {
    return (
      <div className="auth-shell">
        <div className="auth-card compact">
          <div className="muted">加载中…</div>
        </div>
      </div>
    )
  }

  return (
    <Suspense fallback={<div className="auth-shell"><div className="auth-card compact"><div className="muted">正在打开工作台…</div></div></div>}>
      <ReaderPage settings={settings} onSettingsChange={setSettings} />
    </Suspense>
  )
}
