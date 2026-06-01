// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import React from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export default class AppErrorBoundary extends React.Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Uncaught error:', error, info.componentStack)
  }

  handleReload = () => {
    window.location.reload()
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex min-h-screen flex-col items-center justify-center p-8">
          <h1 className="mb-2 text-2xl font-semibold text-foreground">
            Something went wrong
          </h1>
          <p className="mb-4 text-sm text-muted-foreground">
            An unexpected error occurred. Please reload the page.
          </p>
          {this.state.error && (
            <pre className="mb-6 max-w-xl overflow-auto text-xs text-muted-foreground/50">
              {this.state.error.message}
            </pre>
          )}
          <button
            onClick={this.handleReload}
            className="rounded-lg border border-border bg-transparent px-6 py-2 text-sm text-foreground transition-colors hover:bg-muted"
          >
            Reload page
          </button>
        </div>
      )
    }

    return this.props.children
  }
}
