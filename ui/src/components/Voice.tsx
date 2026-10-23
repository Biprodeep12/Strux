import { useCallback, useEffect, useRef, useState } from "react";
import { CaretDown, CircleNotch, Microphone, SpeakerHigh, SpeakerSlash, Stop } from "@phosphor-icons/react";
import { api, type ReadAloud } from "../lib/api";

/**
 * Voice in the chats, all on this PC.
 *  - speech to text: the mic is recorded by Strux itself and transcribed by a local Whisper model (watcher/stt.py)
 *  - text to speech: Windows' own voices through the browser's speechSynthesis (local voices only)
 */
type SttStatus = { recording: boolean; level: number; elapsed: number; model: string; model_state: string; max_seconds: number };

const post = async <T,>(p: string): Promise<T> => {
  const r = await fetch(p, { method: "POST" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `${r.status}`);
  return body as T;
};

/** Click to talk, click again (or Enter) to stop; Esc throws the recording away. Calls onText with what was said. */
export function MicButton({ onText, disabled, onError, small }: {
  onText: (text: string) => void; disabled?: boolean; onError?: (msg: string) => void; small?: boolean;
}) {
  const [state, setState] = useState<"idle" | "recording" | "working">("idle");
  const [st, setSt] = useState<SttStatus | null>(null);
  const timer = useRef<number>();

  const poll = useCallback(() => {
    window.clearInterval(timer.current);
    timer.current = window.setInterval(() => {
      fetch("/api/stt/status").then((r) => r.json()).then((s: SttStatus) => {
        setSt(s);
        if (!s.recording) setState((x) => (x === "recording" ? "idle" : x));     // hit the length limit
      }).catch(() => {});
    }, 120);
  }, []);
  useEffect(() => () => { window.clearInterval(timer.current); if (state === "recording") post("/api/stt/cancel").catch(() => {}); }, []); // eslint-disable-line

  const start = async () => {
    try { setSt(await post<SttStatus>("/api/stt/start")); setState("recording"); poll(); stopSpeaking(); }
    catch (e: any) { onError?.(e.message || String(e)); }
  };
  const stop = async () => {
    window.clearInterval(timer.current);
    setState("working");
    try {
      const r = await post<{ text: string; note?: string }>("/api/stt/stop");
      if (r.text) onText(r.text); else onError?.("I didn't catch anything. Try again a bit closer to the mic.");
    } catch (e: any) { onError?.(e.message || String(e)); }
    finally { setState("idle"); setSt(null); }
  };
  const cancel = () => { window.clearInterval(timer.current); post("/api/stt/cancel").catch(() => {}); setState("idle"); setSt(null); };

  useEffect(() => {
    if (state !== "recording") return;
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel(); }
      if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); stop(); }
    };
    addEventListener("keydown", key, true);
    return () => removeEventListener("keydown", key, true);
  }); // eslint-disable-line

  const size = small ? "size-4" : "size-4";
  if (state === "recording") {
    const ring = 2 + Math.round((st?.level ?? 0) * 7);
    const secs = Math.floor(st?.elapsed ?? 0);
    return (
      <button type="button" data-testid="mic" className="btn px-2.5 shrink-0 text-white border-transparent gap-1.5 tabular-nums" onClick={stop}
        title="Stop and use the text (Enter) · Esc to cancel"
        style={{ background: "var(--critical)", boxShadow: `0 0 0 ${ring}px color-mix(in oklab, var(--critical) 25%, transparent)`, transition: "box-shadow 90ms" }}>
        <Stop className={size} weight="fill" /><span className="text-[11px]">{Math.floor(secs / 60)}:{String(secs % 60).padStart(2, "0")}</span>
      </button>
    );
  }
  if (state === "working") {
    const prep = st?.model_state === "downloading" || st?.model_state === "loading";
    return (
      <button type="button" data-testid="mic" className="btn px-2.5 shrink-0" disabled title={prep ? "Getting the speech model ready (first time only)…" : "Turning speech into text…"}>
        <CircleNotch className={`${size} animate-spin text-accent`} />
      </button>
    );
  }
  return (
    <button type="button" data-testid="mic" className="btn px-2.5 shrink-0" onClick={start} disabled={disabled} title="Speak instead of typing (runs on this PC)" aria-label="Speak">
      <Microphone className={size} weight="duotone" />
    </button>
  );
}

// ---- text to speech ------------------------------------------------------------------------------
const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;

/** The nicest voice that runs on this PC (never an online one: the text would leave the machine). */
function pickVoice(): SpeechSynthesisVoice | undefined {
  const voices = (synth?.getVoices() ?? []).filter((v) => v.localService && v.lang.toLowerCase().startsWith("en"));
  const rank = (v: SpeechSynthesisVoice) =>
    (/natural/i.test(v.name) ? 0 : 10) + (/en-IN/i.test(v.lang) ? 0 : /en-(GB|US)/i.test(v.lang) ? 1 : 2) + (/(aria|jenny|neerja|zira)/i.test(v.name) ? 0 : 1);
  return voices.sort((a, b) => rank(a) - rank(b))[0];
}

/** What a person would read out: no markdown marks, no code blocks, no ids, links shortened. */
export function speakable(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " (code) ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/https?:\/\/(www\.)?([^/\s]+)\S*/g, "$2")
    .replace(/\*\*|__|[*_#>]/g, "")
    .replace(/#(\d+)\b/g, "number $1")
    .replace(/·/g, ",")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 2500);
}

let current: { id: string; done: () => void } | null = null;

/** Tell Strux a reply is being read out, so "Hey Strux" doesn't wake on Strux's own voice. */
const speaking = (seconds: number) => fetch("/api/voice/speaking", { method: "POST", body: JSON.stringify({ seconds }) }).catch(() => {});

export function stopSpeaking() {
  if (current) speaking(0);
  synth?.cancel();
  current?.done();
  current = null;
}

export function speak(text: string, id = "x", onEnd?: () => void): boolean {
  if (!synth) return false;
  stopSpeaking();
  const u = new SpeechSynthesisUtterance(speakable(text));
  const v = pickVoice();
  if (v) { u.voice = v; u.lang = v.lang; }
  u.rate = 1.05;
  speaking(u.text.split(/\s+/).length / 2.6 + 1);
  const done = () => { if (current?.id === id) { current = null; speaking(0); } onEnd?.(); };
  u.onend = done; u.onerror = done;
  current = { id, done: () => onEnd?.() };
  synth.speak(u);
  return true;
}

export const ttsAvailable = () => !!synth;

// ---- your choice: read replies out loud? (saved in config.yaml, shared by the dashboard and the bar chat) ----
const READ_LABEL: Record<ReadAloud, string> = { off: "Off", voice: "When I speak", always: "Every reply" };

export function useReadAloud(): [ReadAloud, (m: ReadAloud) => void] {
  const [mode, setMode] = useState<ReadAloud>("off");
  useEffect(() => {
    const load = () => api.voice().then((v) => setMode(v.read_aloud)).catch(() => {});
    load();
    addEventListener("focus", load);
    return () => removeEventListener("focus", load);
  }, []);
  const set = (m: ReadAloud) => { setMode(m); if (m === "off") stopSpeaking(); api.setVoice({ read_aloud: m }).catch(() => {}); };
  return [mode, set];
}

export const autoRead = (mode: ReadAloud, voice?: boolean) => mode === "always" || (mode === "voice" && !!voice);

/** "Read aloud: Off ▾" in a chat header. */
export function ReadAloudChooser({ mode, onChange, compact }: { mode: ReadAloud; onChange: (m: ReadAloud) => void; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    addEventListener("mousedown", h);
    return () => removeEventListener("mousedown", h);
  }, [open]);
  if (!synth) return null;
  return (
    <div ref={ref} className="relative">
      <button type="button" data-testid="read-aloud" onClick={() => setOpen(!open)} title={`Read replies out loud: ${READ_LABEL[mode]}`}
        className={`inline-flex items-center gap-1 h-7 px-2 rounded-md text-[11px] whitespace-nowrap shrink-0 hover:bg-panel-2 ${mode === "off" ? "text-ink-3" : "text-accent"}`}>
        {mode === "off" ? <SpeakerSlash className="size-3.5" weight="duotone" /> : <SpeakerHigh className="size-3.5" weight="duotone" />}
        {!compact && READ_LABEL[mode]}<CaretDown className="size-2.5" />
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-30 panel p-1 w-44" style={{ boxShadow: "var(--shadow-lg)" }}>
          <div className="px-2 pt-1 pb-1.5 text-[10.5px] text-ink-3">Read replies aloud</div>
          {(Object.keys(READ_LABEL) as ReadAloud[]).map((m) => (
            <button key={m} type="button" data-testid={`read-${m}`} onClick={() => { onChange(m); setOpen(false); }}
              className={`w-full text-left px-2 py-1.5 rounded-md text-[12px] hover:bg-panel-2 ${m === mode ? "text-accent font-medium" : "text-ink"}`}>{READ_LABEL[m]}</button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Small speaker on a reply: read it out / stop. */
const played = new WeakSet<object>();      // replies already read out by themselves (lists re-mount on tab switches)

export function SpeakButton({ text, id, autoPlay, msg }: { text: string; id: string; autoPlay?: boolean; msg?: object }) {
  const [on, setOn] = useState(false);
  const toggle = () => { if (on) { stopSpeaking(); setOn(false); } else if (speak(text, id, () => setOn(false))) setOn(true); };
  useEffect(() => {
    if (!autoPlay || !msg || played.has(msg)) return;
    played.add(msg);
    // voices load asynchronously the first time
    const go = () => { if (speak(text, id, () => setOn(false))) setOn(true); };
    if (synth && synth.getVoices().length === 0) { synth.addEventListener("voiceschanged", go, { once: true }); setTimeout(go, 400); } else go();
  }, []); // eslint-disable-line
  if (!synth || !text.trim()) return null;
  return (
    <button type="button" data-testid="speak" onClick={toggle} title={on ? "Stop reading" : "Read aloud"}
      className={`inline-flex items-center gap-1 text-[10.5px] rounded-md px-1.5 py-0.5 transition-colors ${on ? "text-accent bg-accent/10" : "text-ink-3 hover:text-ink hover:bg-panel"}`}>
      {on ? <SpeakerSlash className="size-3.5" weight="duotone" /> : <SpeakerHigh className="size-3.5" weight="duotone" />}
      {on ? "Stop" : "Read"}
    </button>
  );
}
