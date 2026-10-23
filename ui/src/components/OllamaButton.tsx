import { useState } from "react";
import { Power } from "@phosphor-icons/react";
import { api } from "../lib/api";

/** "Start Ollama": brings the local model server up (a stuck one is restarted). `onUp` runs once it answers. */
export default function OllamaButton({ onUp, small }: { onUp?: () => void; small?: boolean }) {
  const [state, setState] = useState<"idle" | "starting" | "up" | "error">("idle");
  const [msg, setMsg] = useState("");
  const start = async () => {
    setState("starting"); setMsg("");
    try {
      const r = await api.startOllama();
      if (r.ok) { setState("up"); setMsg(r.note ?? `Up in ${r.seconds} s${r.restarted ? " (restarted a stuck one)" : ""}`); onUp?.(); }
      else { setState("error"); setMsg(r.error ?? "didn't start"); }
    } catch (e: any) { setState("error"); setMsg(String(e)); }
  };
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <button className={`btn ${small ? "py-1 px-2 text-[11.5px]" : ""}`} onClick={start} disabled={state === "starting" || state === "up"} data-testid="ollama-start">
        <Power className={small ? "size-3.5" : "size-4"} weight="duotone" />
        {state === "starting" ? "Starting Ollama…" : state === "up" ? "Ollama is up" : "Start Ollama"}
      </button>
      {msg && <span className={`text-[11px] ${state === "error" ? "text-critical" : "text-ink-3"}`}>{msg}</span>}
    </span>
  );
}
