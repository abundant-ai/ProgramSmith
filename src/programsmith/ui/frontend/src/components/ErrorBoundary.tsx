import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

/** Catches render errors so one bad component shows a message instead of a blank screen. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: unknown) {
    // surface in the console for debugging
    console.error("UI render error:", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="mx-auto mt-16 max-w-lg rounded-xl border border-line bg-surface-2 p-6 text-center">
          <h2 className="text-lg font-semibold text-ink">Something broke rendering this view</h2>
          <p className="mt-2 text-[13px] text-ink-3">{this.state.error.message}</p>
          <div className="mt-4 flex justify-center gap-3">
            <button
              className="rounded-lg bg-accent px-4 py-2 text-[13px] font-medium text-white"
              onClick={() => this.setState({ error: null })}
            >
              Retry
            </button>
            <a
              href="/"
              className="rounded-lg border border-line px-4 py-2 text-[13px] font-medium text-ink-2"
            >
              Back to fleet
            </a>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
