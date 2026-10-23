import { useEffect, useRef, useState } from "react";
import type { Status } from "./api";

export type LiveEvent = { type: "event"; kind: string; ts: number; app: string; title: string; data: Record<string, any> };
export type LiveSuggestion = { type: "suggestion"; id: number; ts: number; source: string; text: string };
export type OverlayMsg = { type: "overlay"; item: "suggestion" | "alert" | "processing" | "focus-chat" | "expanded" | "pane"; [k: string]: any };

type Live = {
  connected: boolean;
  status: Status | null;
  events: LiveEvent[];
  suggestions: LiveSuggestion[];
  overlay: OverlayMsg[];
  planner: number;          // bumps on every TODO / calendar change (any source), pages reload on it
};

/** One shared WebSocket for the whole app with reconnect + keepalive. */
/** opts.eventKinds: keep only these event kinds (the pane only needs Claude events, so other events cost it nothing). */
export function useLive(opts?: { eventKinds?: string[] }): Live {
  const [state, setState] = useState<Live>({ connected: false, status: null, events: [], suggestions: [], overlay: [], planner: 0 });
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let closed = false;
    let retry = 1000;
    let ping: number | undefined;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      wsRef.current = ws;
      ws.onopen = () => {
        retry = 1000;
        setState((s) => ({ ...s, connected: true }));
        ping = window.setInterval(() => ws.readyState === 1 && ws.send("ping"), 20000);
      };
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data);
        if (msg.type === "event" && opts?.eventKinds && !opts.eventKinds.includes(msg.kind)) return;
        setState((s) => {
          if (msg.type === "status") return { ...s, status: msg };
          if (msg.type === "planner") return { ...s, planner: s.planner + 1 };
          if (msg.type === "event") return { ...s, events: [msg, ...s.events].slice(0, 80) };
          if (msg.type === "suggestion") return { ...s, suggestions: [msg, ...s.suggestions].slice(0, 20) };
          if (msg.type === "overlay") return { ...s, overlay: [{ ...msg, _n: Date.now() + Math.random() }, ...s.overlay].slice(0, 20) };
          return s;
        });
      };
      ws.onclose = () => {
        window.clearInterval(ping);
        setState((s) => ({ ...s, connected: false }));
        if (!closed) {
          setTimeout(connect, retry);
          retry = Math.min(retry * 2, 15000);
        }
      };
      ws.onerror = () => ws.close();
    };
    connect();
    return () => { closed = true; window.clearInterval(ping); wsRef.current?.close(); };
  }, []);

  return state;
}
