import { Component, type ErrorInfo, type ReactNode } from 'react'

// No ErrorBoundary existed anywhere in the app (finding 10) — a render throw
// white-screened it with zero indication of what happened. This is the
// last-resort catch: normal fetch failures are handled inline (App.tsx's
// `loadError` banner) — this only fires for actual render-time bugs.
interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Unhandled render error:', error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-screen flex-col items-center justify-center gap-3 bg-background p-8 text-center">
          <p className="text-sm font-medium text-destructive">Something went wrong rendering the page.</p>
          <p className="max-w-md text-xs text-muted-foreground">{this.state.error.message}</p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="rounded-lg border border-input px-3 py-1.5 text-xs hover:bg-muted"
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
