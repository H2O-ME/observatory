/**
 * The last line of defence: a render error used to blank the whole console.
 *
 * It shows what failed and offers a reload, so an operator on the watch browser
 * is never left with a white page and no way forward.
 */

import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

import { Button } from "@/components/ui/button";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Keep the trace in the console for the operator's own debugging; the UI
    // stays deliberately short.
    console.error("observatory console crashed", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="flex app-vh-min items-center justify-center px-4">
        <div className="glass w-full max-w-md rounded-panel border border-line p-5 text-center shadow-card">
          <p className="text-sm font-medium text-ink">控制台渲染出错</p>
          <p className="mt-1.5 break-words text-xs leading-relaxed text-soft">{error.message}</p>
          <Button className="mt-4 w-full" onClick={() => window.location.reload()}>
            重新加载
          </Button>
        </div>
      </div>
    );
  }
}