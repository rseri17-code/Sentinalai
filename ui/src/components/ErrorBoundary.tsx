import React from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

interface Props {
  children: React.ReactNode
  label?: string
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[ErrorBoundary]', this.props.label ?? 'panel', error, info.componentStack)
  }

  render() {
    if (!this.state.error) return this.props.children

    return (
      <div className="flex flex-col items-center justify-center h-full p-8 text-center space-y-4">
        <AlertTriangle size={32} className="text-red-400" />
        <div>
          <div className="text-slate-200 font-medium">
            {this.props.label ?? 'This panel'} encountered an error
          </div>
          <div className="text-slate-500 text-sm mt-1 font-mono max-w-md truncate">
            {this.state.error.message}
          </div>
        </div>
        <button
          onClick={() => this.setState({ error: null })}
          className="flex items-center gap-2 px-4 py-2 bg-slate-800 hover:bg-slate-700
                     text-slate-300 text-sm rounded-lg transition-colors"
        >
          <RefreshCw size={13} />
          Retry
        </button>
      </div>
    )
  }
}
