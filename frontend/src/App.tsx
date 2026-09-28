import { Component, lazy, Suspense, useEffect, useState, type ReactNode } from 'react'
import { getSettings, type UserSettings } from './lib/api'

const ReaderPage = lazy(() => import('./pages/ReaderPage').then((module) => ({ default: module.ReaderPage })))

// A lazy chunk that fails to evaluate (e.g. a browser kernel too old for the
// bundle) rejects the import and, without a boundary, unmounts the whole tree,
// leaving a blank page. Surface the failure with a readable message instead.
class ReaderErrorBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null }

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error.message : String(error) }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="auth-shell">
          <div className="auth-card compact">
            <h2>界面加载失败</h2>
            <p className="muted">{this.state.error}</p>
            <p className="muted">如果使用的是较旧的浏览器或 WebView2 内核，请升级到最新版后重试。</p>
            <button className="btn primary" onClick={() => window.location.reload()}>重新加载</button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

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
    <ReaderErrorBoundary>
      <Suspense fallback={<div className="auth-shell"><div className="auth-card compact"><div className="muted">正在打开工作台…</div></div></div>}>
        <ReaderPage settings={settings} onSettingsChange={setSettings} />
      </Suspense>
    </ReaderErrorBoundary>
  )
}
