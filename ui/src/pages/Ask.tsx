import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { ArrowUp, User, Warning } from "@phosphor-icons/react";
import { useLiveCtx } from "../App";
import { api } from "../lib/api";
import { StruxMark } from "../components/Brand";
import { PageHeader } from "../components/Bits";
import type { Change } from "../lib/api";
import ChangeChips from "../components/ChangeChips";
import ModelTag from "../components/ModelTag";
import OllamaButton from "../components/OllamaButton";
import { AttachButton, AttachmentChips, DropHint, useAttachments } from "../components/Attachments";
import type { Attachment } from "../lib/attach";
import { MicButton, ReadAloudChooser, SpeakButton, autoRead, useReadAloud } from "../components/Voice";

type Msg = { role: "user" | "assistant"; content: string; attachments?: Attachment[]; latency?: number; error?: boolean; changes?: Change[]; model?: string; voice?: boolean; fresh?: boolean };

const STARTERS = [
  "What did I work on in the last hour?",
  "How much time did I spend in the browser today?",
  "What is running in the background right now?",
  "What is Claude Code doing?",
  "Plan my day",
  "Add a TODO to review the PR tomorrow at 11",
];

export default function Ask() {
  const { status } = useLiveCtx();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [conv] = useState(() => `a${Date.now().toString(36)}`);     // kept in the chat history, so Strux learns from it
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const [models, setModels] = useState<{ reachable: boolean; models: string[] } | null>(null);

  useEffect(() => { api.models().then(setModels).catch(() => setModels({ reachable: false, models: [] })); }, []);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, busy]);
  const wanted = status?.llm?.model ?? "";
  const missing = models && models.reachable && wanted && !models.models.some((m) => m === wanted || m.split(":")[0] === wanted.split(":")[0]);

  const att = useAttachments((msg) => setMsgs((l) => [...l, { role: "assistant", content: msg, error: true }]));
  const [readMode, setReadMode] = useReadAloud();
  const spoke = useRef(false);                  // this message was dictated (for "read aloud: when I speak")
  const inputEl = useRef<HTMLInputElement>(null);
  const dictated = (text: string) => {
    spoke.current = true;
    setInput((v) => (v.trim() ? `${v.trimEnd()} ${text}` : text));
    setTimeout(() => inputEl.current?.focus(), 0);
  };
  const send = async (text?: string) => {
    const q = (text ?? input).trim();
    const files = text ? [] : att.items;
    if ((!q && !files.length) || busy) return;
    const next: Msg[] = [...msgs, { role: "user", content: q, attachments: files.length ? files : undefined }];
    const voice = !text && spoke.current;
    spoke.current = false;
    setMsgs(next); setInput(""); att.clear(); setBusy(true);
    try {
      const r = await api.chat(next.map(({ role, content, attachments }) => ({ role, content, attachments: attachments?.map(({ name, type, data }) => ({ name, type, data })) })), conv);
      setMsgs([...next, r.ok ? { role: "assistant", content: r.text!, latency: r.latency, changes: r.changes, model: r.model, voice, fresh: true } : { role: "assistant", content: r.error ?? "Failed", error: true }]);
    } catch (e: any) { setMsgs([...next, { role: "assistant", content: String(e), error: true }]); }
    finally { setBusy(false); }
  };

  return (
    <div className="h-[calc(100vh-48px)] flex flex-col">
      <PageHeader title="Ask" subtitle="Your activity, TODOs, calendar, files and screenshots, answered on this PC."
        right={<div className="flex items-center gap-2"><ReadAloudChooser mode={readMode} onChange={setReadMode} /><span className="panel px-2.5 py-1 shadow-none"><ModelTag model={status?.llm?.model} className="text-[11px]" /></span></div>} />

      {models && (!models.reachable || missing) && (
        <div className="mb-3 flex items-start gap-3 rounded-xl border border-warn/30 bg-warn/10 px-4 py-3 text-sm">
          <Warning className="size-4 text-warn mt-0.5 shrink-0" weight="duotone" />
          <div>
            {!models.reachable ? <div className="space-y-2"><div>Ollama is not running. Start it and the model features switch on automatically.</div>
              <OllamaButton onUp={() => api.models().then(setModels).catch(() => {})} /></div> :
              <>Model <span className="font-mono">{wanted}</span> is not installed. Run <span className="font-mono">ollama pull {wanted}</span>. Installed: {models.models.join(", ") || "none"}.</>}
          </div>
        </div>
      )}
      <div className="panel flex-1 min-h-0 flex flex-col overflow-hidden relative">
        <DropHint show={att.dragging} />
        <div className="flex-1 overflow-y-auto p-5 space-y-4">
          {msgs.length === 0 && (
            <div className="h-full grid place-items-center">
              <div className="max-w-md text-center space-y-4">
                <div className="mx-auto w-fit"><StruxMark size={48} /></div>
                <div className="text-sm text-ink-2">Try one of these</div>
                <div className="grid gap-2">
                  {STARTERS.map((s) => <button key={s} className="btn justify-start text-left" onClick={() => send(s)}>{s}</button>)}
                </div>
              </div>
            </div>
          )}
          {msgs.map((m, i) => (
            <motion.div key={i} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} className={`flex gap-3 ${m.role === "user" ? "justify-end" : ""}`}>
              {m.role === "assistant" && <div className="shrink-0"><StruxMark size={32} /></div>}
              <div className={`max-w-[75%] rounded-2xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap ${m.role === "user" ? "text-white rounded-br-md" : m.error ? "bg-critical/10 border border-critical/30 text-critical rounded-bl-md" : "bg-panel-2 border border-line rounded-bl-md"}`} style={m.role === "user" ? { background: "var(--accent)" } : undefined}>
                {m.attachments?.length ? <div className={m.content ? "mb-2" : ""}><AttachmentChips items={m.attachments} small /></div> : null}
                {m.content}
                <ChangeChips changes={m.changes} />
                {m.role === "assistant" && !m.error && <div className="mt-2 flex items-center gap-2"><ModelTag model={m.model ?? status?.llm?.model} latency={m.latency} /><SpeakButton text={m.content} id={`ask-${i}`} msg={m} autoPlay={m.fresh && autoRead(readMode, m.voice)} /></div>}
              </div>
              {m.role === "user" && <div className="size-8 rounded-xl bg-panel-2 grid place-items-center shrink-0"><User className="size-4 text-ink-2" weight="duotone" /></div>}
            </motion.div>
          ))}
          {busy && (
            <div className="flex gap-3">
              <div className="shrink-0"><StruxMark size={32} /></div>
              <div className="bg-panel-2 border border-line rounded-2xl rounded-bl-md px-4 py-3 flex gap-1 items-center">
                <span className="size-1.5 rounded-full bg-ink-3 live-dot" /><span className="size-1.5 rounded-full bg-ink-3 live-dot [animation-delay:.2s]" /><span className="size-1.5 rounded-full bg-ink-3 live-dot [animation-delay:.4s]" />
              </div>
            </div>
          )}
          <div ref={bottom} />
        </div>
        <form className="border-t border-line p-3 space-y-2" onSubmit={(e) => { e.preventDefault(); send(); }}>
          {att.inputEl}
          <AttachmentChips items={att.items} onRemove={att.remove} />
          <div className="flex gap-2">
            <AttachButton onClick={att.open} disabled={busy} />
            <MicButton onText={dictated} disabled={busy} onError={(msg) => setMsgs((l) => [...l, { role: "assistant", content: msg, error: true }])} />
            <input ref={inputEl} data-testid="ask-input" value={input} onChange={(e) => setInput(e.target.value)} disabled={busy} className="input"
              placeholder={att.items.length ? "Ask about the attachment… (or just send)" : "Ask about your day, or attach / paste / drop a file or screenshot…"} />
            <button data-testid="ask-send" className="btn btn-primary" disabled={busy || (!input.trim() && !att.items.length)}><ArrowUp className="size-4" weight="bold" /></button>
          </div>
        </form>
      </div>
    </div>
  );
}
