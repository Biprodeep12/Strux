import { Component, type ReactNode } from "react";
import { ArrowClockwise, WarningCircle } from "@phosphor-icons/react";

/** A page that throws while rendering shows this card instead of blanking the whole window. */
export default class ErrorBoundary extends Component<{ children: ReactNode; name?: string }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    fetch("/api/client-error", { method: "POST", body: JSON.stringify({ page: this.props.name, message: String(error?.stack || error).slice(0, 2000) }) }).catch(() => {});
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="panel p-6 max-w-xl mx-auto mt-10 text-center">
        <WarningCircle className="size-8 mx-auto text-warn" weight="duotone" />
        <div className="mt-2 font-medium">This page hit an error</div>
        <div className="mt-1 text-xs text-ink-3 break-words">{this.state.error.message}</div>
        <button className="btn mt-4 mx-auto" onClick={() => this.setState({ error: null })}><ArrowClockwise className="size-4" /> Try again</button>
      </div>
    );
  }
}
