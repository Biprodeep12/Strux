import { useEffect, useRef, useState, type ReactNode } from "react";
import { motion } from "framer-motion";
import { ArrowSquareOut, CaretUp, Clock, Cpu, Keyboard, Microphone, Pause, Play, Robot, Sparkle, Stack, Trash } from "@phosphor-icons/react";
import { useLiveCtx, useToast } from "../App";
import { api } from "../lib/api";
import { fmtDur, modelLabel } from "../lib/format";
import { Wordmark } from "./Brand";

export type NavItem = { id: string; label: string; icon: (props: { className?: string; weight?: any }) => ReactNode };

export default function Sidebar({ items, active, onSelect }: { items: NavItem[]; active: string; onSelect: (id: string) => void }) {
  const { connected, status } = useLiveCtx();
  const toast = useToast();
  const paused = status?.paused ?? false;

  const togglePause = async () => {
    const r = await api.pause(!paused);
    toast({ title: r.paused ? "Paused" : "Watching again", body: r.paused ? "Sensors and brain are idle." : "Sensors are recording.", kind: r.paused ? "warn" : "good" });
  };
  const forget = async () => {
    const r = await api.forget(10);
    toast({ title: "Forgot last 10 minutes", body: `${r.events} events, ${r.frames} frames, ${r.files} files removed`, kind: "warn" });
  };
  const think = async () => { await api.think(); toast({ title: "Thinking…", body: "Rules and the model run on the next tick.", kind: "accent" }); };

  return (
    <aside className="w-[240px] shrink-0 h-full border-r border-line bg-panel/70 backdrop-blur flex flex-col">
      <div className="px-5 pt-5 pb-4">
        <Wordmark sub={connected ? (paused ? "paused" : "watching") : "disconnected"} />
      </div>

      <nav className="px-3 mt-1 space-y-0.5">
        {items.map(({ id, label, icon: Icon }) => {
          const on = active === id;
          return (
            <button key={id} onClick={() => onSelect(id)}
              className={`relative w-full flex items-center gap-3 px-3 py-2 rounded-xl text-[13.5px] transition-colors ${on ? "text-ink font-medium" : "text-ink-2 hover:text-ink"}`}>
              {on && <motion.span layoutId="nav-pill" className="absolute inset-0 rounded-xl" style={{ background: "color-mix(in oklab, var(--accent) 13%, transparent)" }} transition={{ type: "spring", stiffness: 500, damping: 40 }} />}
              <span className={`relative ${on ? "text-accent" : ""}`}><Icon className="size-[18px]" weight={on ? "fill" : "duotone"} /></span>
              <span className="relative">{label}</span>
              {id === "suggestions" && status && status.suggestions > 0 && <span className="relative ml-auto text-[11px] text-ink-3 font-mono">{status.suggestions}</span>}
            </button>
          );
        })}
      </nav>

      <div className="mt-auto p-3 space-y-3">
        {status && (
          <div className="rounded-xl border border-line bg-panel px-3 py-2.5 space-y-2" data-testid="sidebar-status">
            <StatusRow icon={<Robot className="size-3.5" weight="duotone" />} label="Model" title={status.llm ? `${status.llm.model} · runs locally` : ""}
              value={<span className={status.llm_error ? "text-critical" : ""}>{status.llm ? modelLabel(status.llm.model) : "—"}</span>} />
            {status.gpu && <StatusRow icon={<Cpu className="size-3.5" weight="duotone" />} label="GPU" value={`${status.gpu.util}% · ${Math.round(status.gpu.free_mb / 102.4) / 10} GB free`} />}
            <StatusRow icon={<Clock className="size-3.5" weight="duotone" />} label="Uptime" value={fmtDur(status.uptime)} />
            {status.wake?.enabled && (
              <StatusRow icon={<Microphone className={`size-3.5 ${status.wake.state === "waiting" ? "text-accent" : ""}`} weight="duotone" />} label="Voice"
                title="The microphone is open for “Hey Strux”. Switch it off in Settings → Voice."
                value={<span className={status.wake.state.startsWith("error") ? "text-critical" : "text-accent"}>{status.wake.state.startsWith("error") ? "mic error" : status.wake.state === "listening" ? "listening…" : "Hey Strux on"}</span>} />
            )}
            {(status.queue > 0 || status.dropped > 0) && (
              <StatusRow icon={<Stack className="size-3.5" weight="duotone" />} label="Queue" value={<span className={status.dropped ? "text-warn" : ""}>{status.queue} · {status.dropped} dropped</span>} />
            )}
          </div>
        )}
        <div className="grid grid-cols-4 gap-2">
          <IconBtn title={paused ? "Resume (Ctrl+Alt+P)" : "Pause (Ctrl+Alt+P)"} onClick={togglePause}>{paused ? <Play className="size-4" weight="duotone" /> : <Pause className="size-4" weight="duotone" />}</IconBtn>
          <IconBtn title="Think now" onClick={think}><Sparkle className="size-4" weight="duotone" /></IconBtn>
          <IconBtn title="Forget last 10 min (Ctrl+Alt+F)" onClick={forget}><Trash className="size-4" weight="duotone" /></IconBtn>
          <IconBtn title="Open in browser" onClick={() => window.open(location.origin, "_blank")}><ArrowSquareOut className="size-4" weight="duotone" /></IconBtn>
        </div>
        <Shortcuts />
      </div>
    </aside>
  );
}

function IconBtn({ title, onClick, children }: { title: string; onClick: () => void; children: ReactNode }) {
  return (
    <motion.button whileTap={{ scale: 0.94 }} className="btn justify-center px-0 py-2" title={title} onClick={onClick}>{children}</motion.button>
  );
}

function StatusRow({ icon, label, value, title }: { icon: ReactNode; label: string; value: ReactNode; title?: string }) {
  return (
    <div className="flex items-center gap-2 text-[12px]" title={title}>
      <span className="text-ink-3 shrink-0">{icon}</span>
      <span className="text-ink-3">{label}</span>
      <span className="ml-auto text-ink-2 font-medium truncate tabular-nums">{value}</span>
    </div>
  );
}

const SHORTCUTS: [string[], string][] = [
  [["Ctrl", "Alt", "Space"], "Chat in the bar"],
  [["Ctrl", "Alt", "V"], "Ask about what you copied (image, screenshot, files)"],
  [["Ctrl", "Alt", "W"], "Show / hide dashboard"],
  [["Ctrl", "Alt", "P"], "Pause watching"],
  [["Ctrl", "Alt", "F"], "Forget last 10 min"],
  [["/"], "Search"],
  [["g", "d"], "Dashboard · g o TODOs · g l calendar · g m memory · g t timeline · g c settings"],
];

/** Keyboard shortcuts behind one quiet button instead of a cramped line of key caps. */
function Shortcuts() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    addEventListener("mousedown", h);
    return () => removeEventListener("mousedown", h);
  }, [open]);
  return (
    <div ref={ref} className="relative">
      <button className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-[12px] text-ink-3 hover:text-ink hover:bg-panel-2 transition-colors" onClick={() => setOpen(!open)}>
        <Keyboard className="size-4" weight="duotone" />Keyboard shortcuts
        <CaretUp className={`size-3 ml-auto transition-transform ${open ? "" : "rotate-180"}`} />
      </button>
      {open && (
        <div className="absolute bottom-full mb-2 inset-x-0 panel p-3 space-y-2.5 z-20" style={{ boxShadow: "var(--shadow-lg)" }}>
          {SHORTCUTS.map(([keys, what]) => (
            <div key={what} className="text-[12px]">
              <div className="flex items-center gap-1">{keys.map((k) => <span key={k} className="kbd">{k}</span>)}</div>
              <div className="text-ink-2 leading-snug mt-1">{what}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
