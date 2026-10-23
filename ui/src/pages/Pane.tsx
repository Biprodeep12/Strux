import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ArrowLeft, ArrowUp, ChatCircleDots, Check, ClockCountdown, ClockCounterClockwise, Cpu, Eye, Info, ListBullets, ListChecks, NotePencil, Robot, Sparkle,
  ThumbsDown, ThumbsUp, Trash, Warning, X } from "@phosphor-icons/react";
import { useLiveCtx } from "../App";
import { api } from "../lib/api";
import { actionLabel, ago, appLabel } from "../lib/format";
import type { Change, ChatSummary, ScreenLook, StoredMessage, Todo } from "../lib/api";
import { QuickAdd, TodoRow, addDays, useTodos } from "../components/Planner";
import ChangeChips from "../components/ChangeChips";
import ModelTag from "../components/ModelTag";
import OllamaButton from "../components/OllamaButton";
import { AttachButton, AttachmentChips, DropHint, useAttachments } from "../components/Attachments";
import type { Attachment } from "../lib/attach";
import { MicButton, ReadAloudChooser, SpeakButton, autoRead, useReadAloud } from "../components/Voice";

type Sugg = { id: number; ts: number; source: string; text: string; context?: string; action?: string };
type Alert = { key: string; ts: number; level: string; text: string };
type Chat = { role: "user" | "assistant"; content: string; attachments?: Attachment[]; error?: boolean; ollamaDown?: boolean; pending?: boolean; thread?: string; changes?: Change[]; model?: string | null; latency?: number; voice?: boolean; fresh?: boolean;
  id?: number | null; rating?: number; screen?: ScreenLook | null };
type ActionMsg = { id: number; thread: string; content: string; pending?: boolean; error?: boolean; ts: number; model?: string | null };
type ClaudeSession = { id: string; project: string; status: string; last: string; waiting: boolean; updated: number };
type Tab = "chat" | "panel" | "todos";
const TABS: Tab[] = ["chat", "panel", "todos"];

const post = (p: string, body: any = {}) => fetch(p, { method: "POST", body: JSON.stringify(body) }).catch(() => {});
const maxHeight = () => Math.round(Math.min(560, (window.screen?.availHeight || 900) * 0.7));
const newConv = () => `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
const RESTORE_HOURS = 6;                          // reopening the pane within this brings the last conversation back
// what the backend will do for this question, so the wait says it ("Looking at your screen…")
const SCREEN_HINT = /\b(on|in|at|of) (my|the) (screen|display|monitor)\b|\bmy screen\b|\bwhat('s| is| am i)\s+(i'?m\s+)?(looking at|seeing|reading|watching)\b|\b(read|see|look at|check|describe|explain|summari[sz]e) (my |the |this |that )?(screen|page|window|tab|error|code|sheet|spreadsheet|table|document|doc|email|chart|dialog)\b|\bwhat('s| is) (in|on) this\b|\bcan you see\b/i;
const PLANNER_HINT = /\b(todo|to-do|task|remind|calendar|meeting|event|schedule|deadline)s?\b/i;
const CLAUDE_HINT = /\bclaude\b/i;
function busyHint(q: string, look: boolean) {
  if (look || SCREEN_HINT.test(q)) return "Looking at your screen…";
  if (PLANNER_HINT.test(q)) return "Checking your TODOs and calendar…";
  if (CLAUDE_HINT.test(q)) return "Reading your Claude Code sessions…";
  return "Thinking…";
}
const fromStored = (m: StoredMessage): Chat => ({ role: m.role, content: m.content, id: m.role === "assistant" ? m.id : undefined, rating: m.rating,
  model: m.meta?.model, latency: m.meta?.latency, changes: m.meta?.changes, screen: m.meta?.screen ?? null,
  attachments: m.meta?.attachments?.map((name): Attachment => ({ name, type: "", data: "", size: 0 })) });

/**
 * The drop-down under the native Strux bar: chat + activity, in its own (hidden-when-closed) window.
 * Layout: tabs and the chat input are pinned; only the list in between scrolls. The page asks the
 * backend to size the window to its natural content height, capped at maxHeight().
 */
export default function Pane() {
  const { overlay, events } = useLiveCtx();
  const [tab, setTab] = useState<Tab>("chat");
  const [sugg, setSugg] = useState<Sugg[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [claude, setClaude] = useState<ClaudeSession[]>([]);
  const [hot, setHot] = useState<string[]>([]);
  const [user, setUser] = useState("");
  const [model, setModel] = useState("");
  const [chat, setChat] = useState<Chat[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState("Thinking…");
  const [shown, setShown] = useState(0);
  const [conv, setConv] = useState(newConv);
  const [view, setView] = useState<"chat" | "history">("chat");
  const [history, setHistory] = useState<ChatSummary[] | null>(null);
  const [chips, setChips] = useState<string[]>([]);
  const [front, setFront] = useState<{ app?: string; title?: string }>({});
  const { todos } = useTodos();
  const [editing, setEditing] = useState(false);          // a TODO's editor is open: use the full height
  const inputRef = useRef<HTMLInputElement>(null);
  const headRef = useRef<HTMLDivElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);

  // results of accepted suggestions arrive live AND are listed in /api/overlay/state (the page may not have been
  // running when they were sent), so merge by id; a final message replaces the pending one of its thread
  const seenChat = useRef<Set<number>>(new Set());
  const ingest = useCallback((msgs: ActionMsg[]) => {
    const fresh = msgs.filter((m) => m && !seenChat.current.has(m.id)).sort((a, b) => a.id - b.id);
    if (!fresh.length) return;
    fresh.forEach((m) => seenChat.current.add(m.id));
    setChat((c) => fresh.reduce((acc, m) => [...acc.filter((x) => !(x.pending && x.thread === m.thread)),
      { role: "assistant" as const, content: m.content, error: m.error, pending: m.pending, thread: m.thread, model: m.model }], c));
    setTab("chat");
  }, []);

  const seenVoice = useRef<Set<number>>(new Set());
  const busyRef = useRef(false);
  const voiceQueue = useRef<string[]>([]);
  const sendRef = useRef<(said: string) => void>(() => {});
  const ingestVoice = useCallback((items: { id: number; ts: number; text: string }[]) => {
    for (const v of items) {
      if (!v || seenVoice.current.has(v.id)) continue;
      seenVoice.current.add(v.id);
      if (Date.now() / 1000 - v.ts < 60) {
        setTab("chat");
        if (busyRef.current) voiceQueue.current = [...voiceQueue.current, v.text].slice(-2);   // answered next, not dropped
        else sendRef.current(v.text);
      }
    }
  }, []);

  const seenAttach = useRef<Set<number>>(new Set());
  const attRef = useRef<{ addReady: (l: Attachment[]) => void } | null>(null);
  const ingestAttach = useCallback((items: { id: number; attachments: Attachment[] }[]) => {
    for (const it of items) {
      if (!it || seenAttach.current.has(it.id)) continue;
      seenAttach.current.add(it.id);
      attRef.current?.addReady(it.attachments);
    }
  }, []);

  const load = useCallback(() => fetch("/api/overlay/state").then((r) => r.json()).then((s) => {
    setSugg(s.suggestions ?? []); setAlerts((s.alerts ?? []).slice(-3)); setClaude(s.claude ?? []); setHot(s.hot ?? []);
    ingest(s.chat ?? []); ingestAttach(s.attach ?? []); ingestVoice(s.voice ?? []); if (s.user) setUser(s.user); if (s.model) setModel(s.model);
    if (Array.isArray(s.chips)) setChips(s.chips); if (s.front) setFront(s.front);
    if (TABS.includes(s.pane)) setTab(s.pane);
  }).catch(() => {}), [ingest, ingestAttach]);

  const restored = useRef(false);
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    api.chats(1).then(async ({ chats }) => {
      const c = chats[0];
      if (!c || Date.now() / 1000 - c.updated > RESTORE_HOURS * 3600) return;
      const { messages } = await api.conversation(c.conv);
      setChat((cur) => (cur.length ? cur : messages.map(fromStored)));
      setConv((cur) => cur === c.conv ? cur : c.conv);
    }).catch(() => {});
  }, []);
  const openConv = async (id: string) => {
    try { const { messages } = await api.conversation(id); setChat(messages.map(fromStored)); setConv(id); setView("chat"); }
    catch { /* gone */ }
  };
  const showHistory = () => {
    if (view === "history") { setView("chat"); return; }
    setView("history"); setHistory(null);
    api.chats(20).then((r) => setHistory(r.chats)).catch(() => setHistory([]));
  };
  const newChat = () => { setChat([]); setConv(newConv()); setView("chat"); setTimeout(() => inputRef.current?.focus(), 0); };
  const rate = (i: number, rating: 1 | -1 | 0, why = "") => {
    const m = chat[i];
    if (!m?.id) return;
    setChat((c) => c.map((x, j) => (j === i ? { ...x, rating } : x)));
    api.rateChat(m.id, rating, why).catch(() => {});
  };

  useEffect(() => {
    load();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") post("/api/overlay/close"); };
    const onBlur = () => post("/api/pane/blur");
    const onFocus = () => { load(); setShown((n) => n + 1); };
    addEventListener("keydown", onKey); addEventListener("blur", onBlur); addEventListener("focus", onFocus);
    return () => { removeEventListener("keydown", onKey); removeEventListener("blur", onBlur); removeEventListener("focus", onFocus); };
  }, [load]);

  // live messages exactly once
  const seen = useRef<Set<number>>(new Set());
  useEffect(() => {
    for (const m of (overlay as any[]).filter((m) => !seen.current.has(m._n)).reverse()) {
      seen.current.add(m._n);
      if (m.item === "pane") { setTab(m.tab); setShown((n) => n + 1); load(); }
      if (m.item === "focus-chat") { setTab("chat"); setTimeout(() => inputRef.current?.focus(), 80); }
      if (m.item === "suggestion" || m.item === "alert") load();
      if (m.item === "chat") ingest([m as ActionMsg]);
      if (m.item === "attach") ingestAttach([m as any]);           // files dropped on the bar / Ctrl+Alt+V
      if (m.item === "voice") ingestVoice([m as any]);             // "Hey Strux, …"
    }
  }, [overlay, load, ingest]);
  const lastClaude = events.find((e) => e.kind === "claude");
  useEffect(() => { if (lastClaude) load(); // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastClaude?.ts]);

  useEffect(() => { if (tab === "chat") setTimeout(() => inputRef.current?.focus(), 60); }, [tab, shown]);
  useEffect(() => { if (tab !== "chat") setView("chat"); }, [tab]);

  // keep the newest message in view by scrolling the LIST only (never the page)
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && tab === "chat") requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
  }, [chat, busy, tab, view]);

  // natural height = pinned parts + full list content, capped; the window is resized to it
  useEffect(() => {
    let last = 0, t: number | undefined;
    const report = () => {
      const head = headRef.current?.offsetHeight ?? 0;
      const form = formRef.current?.offsetHeight ?? 0;
      const inner = innerRef.current?.scrollHeight ?? 0;
      const h = editing ? maxHeight() : Math.min(maxHeight(), Math.ceil(head + form + inner + 2));
      if (Math.abs(h - last) < 4) return;
      last = h;
      window.clearTimeout(t);
      t = window.setTimeout(() => post("/api/overlay/size", { height: h }), 20);
    };
    const ro = new ResizeObserver(report);
    [headRef.current, formRef.current, innerRef.current].forEach((el) => el && ro.observe(el));
    report();
    return () => { ro.disconnect(); window.clearTimeout(t); };
  }, [tab, shown, view, editing]);
  useEffect(() => { if (tab !== "todos") setEditing(false); }, [tab]);

  const feedback = async (s: Sugg, value: "accept" | "dismiss" | "snooze") => {
    setSugg((l) => l.filter((x) => x.id !== s.id));
    await api.feedback(s.id, value).catch(() => {});
  };
  const att = useAttachments((msg) => setChat((c) => [...c, { role: "assistant", content: msg, error: true }]),
    () => { setTab("chat"); setTimeout(() => inputRef.current?.focus(), 50); });
  attRef.current = att;
  const [readMode, setReadMode] = useReadAloud();
  const spoke = useRef(false);                  // this message was dictated (for "read aloud: when I speak")
  const dictated = (text: string) => {
    spoke.current = true;
    setTab("chat");
    setInput((v) => (v.trim() ? `${v.trimEnd()} ${text}` : text));
    setTimeout(() => inputRef.current?.focus(), 0);
  };
  const send = async (said?: string, look = false, base: Chat[] = chat) => {
    const q = (said ?? input).trim() || (look ? "What's on my screen?" : "");
    if ((!q && !att.items.length) || busy) return;
    const voice = (said != null && !look && !chipSent.current) || spoke.current;
    spoke.current = false; chipSent.current = false;
    const next: Chat[] = [...base.filter((m) => !m.pending), { role: "user", content: q, attachments: att.items.length ? att.items : undefined }];
    setChat(next); setInput(""); att.clear(); setBusy(true); setView("chat"); setHint(busyHint(q, look));
    try {
      const r = await api.chat(next.map(({ role, content, attachments }) => ({ role, content, attachments: attachments?.filter((a) => a.data).map(({ name, type, data }) => ({ name, type, data })) })), conv, look);
      setChat([...next, r.ok ? { role: "assistant", content: r.text!, changes: r.changes, model: r.model, latency: r.latency, voice, fresh: true, id: r.id, screen: r.screen }
        : { role: "assistant", content: r.error ?? "Failed", error: true, ollamaDown: r.ollama_down }]);
    } catch (e: any) { setChat([...next, { role: "assistant", content: String(e), error: true }]); }
    finally { setBusy(false); setTimeout(() => inputRef.current?.focus(), 0); }
  };

  sendRef.current = (said: string) => { send(said); };
  busyRef.current = busy;
  useEffect(() => {                              // a "Hey Strux" that arrived mid-answer
    if (busy || !voiceQueue.current.length) return;
    const [said, ...rest] = voiceQueue.current;
    voiceQueue.current = rest;
    sendRef.current(said);
  }, [busy]);
  const retry = (i: number) => {                 // Ollama is back: ask the question that failed again
    const q = chat[i - 1];
    if (q?.role === "user") send(q.content, false, chat.slice(0, i - 1));
  };
  const chipSent = useRef(false);
  const ask = (q: string) => { chipSent.current = true; send(q); };
  const count = sugg.length + alerts.length;
  const dueNow = (todos ?? []).filter((t) => t.done_ts == null && t.due_ts != null && t.due_ts < addDays(new Date(), 1).getTime() / 1000).length;

  return (
    <motion.div key={shown} initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.16, ease: "easeOut" }}
      className="relative h-screen w-screen flex flex-col overflow-hidden bg-panel">
      <DropHint show={att.dragging} />
      {/* pinned: tabs */}
      <div ref={headRef} className="shrink-0 flex items-center gap-0.5 px-2 pt-2 pb-1 whitespace-nowrap min-w-0">
        <TabBtn active={tab === "chat"} onClick={() => setTab("chat")} icon={<ChatCircleDots className="size-4" weight={tab === "chat" ? "fill" : "regular"} />}>Chat</TabBtn>
        <TabBtn active={tab === "panel"} onClick={() => setTab("panel")} icon={<ListBullets className="size-4" weight={tab === "panel" ? "fill" : "regular"} />}>
          Activity{count > 0 && <span className="ml-1 min-w-4 h-4 px-1 rounded-full text-[10px] grid place-items-center text-white" style={{ background: "var(--accent)" }}>{count}</span>}
        </TabBtn>
        <TabBtn active={tab === "todos"} onClick={() => setTab("todos")} icon={<ListChecks className="size-4" weight={tab === "todos" ? "fill" : "regular"} />}>
          TODOs{dueNow > 0 && <span title="Due today or overdue" className="ml-1 min-w-4 h-4 px-1 rounded-full text-[10px] grid place-items-center text-white" style={{ background: "var(--accent)" }}>{dueNow}</span>}
        </TabBtn>
        <span className="flex-1 min-w-2" />
        <ModelTag model={model} className="mr-1.5 hidden min-[420px]:inline-flex" />
        {tab === "chat" && <ReadAloudChooser compact mode={readMode} onChange={setReadMode} />}
        {tab === "chat" && <HeadBtn title={view === "history" ? "Back to the chat" : "Recent chats"} active={view === "history"} onClick={showHistory} testid="pane-history">
          {view === "history" ? <ArrowLeft className="size-4" /> : <ClockCounterClockwise className="size-4" />}</HeadBtn>}
        {tab === "chat" && chat.length > 0 && <HeadBtn title="New chat" onClick={newChat} testid="pane-new"><NotePencil className="size-4" /></HeadBtn>}
        <HeadBtn title="Close (Esc)" onClick={() => post("/api/overlay/close")}><X className="size-4" weight="bold" /></HeadBtn>
      </div>

      {/* pinned: chat input */}
      {tab === "chat" && (
        <form ref={formRef} className="shrink-0 px-3 pt-1 pb-2 space-y-2" onSubmit={(e) => { e.preventDefault(); send(); }}>
          {att.inputEl}
          <AttachmentChips items={att.items} onRemove={att.remove} />
          <div className="flex gap-2">
            <AttachButton onClick={att.open} disabled={busy} />
            <MicButton onText={dictated} disabled={busy} small onError={(msg) => setChat((c) => [...c, { role: "assistant", content: msg, error: true }])} />
            <button type="button" data-testid="pane-look" className="btn px-2.5 shrink-0" disabled={busy} onClick={() => send(input.trim() || undefined, true)}
              title="Look at my screen (asks about what you typed, or what's on it)" aria-label="Look at my screen"><Eye className="size-4" /></button>
            <input ref={inputRef} value={input} onChange={(e) => setInput(e.target.value)}
              placeholder={att.items.length ? "Ask about the attachment… (or just send)" : "Ask about your screen, your day, a file…"} className="input py-2.5 text-[13px] min-w-0" disabled={busy} />
            <button className="btn btn-primary px-3" disabled={busy || (!input.trim() && !att.items.length)}><ArrowUp className="size-4" weight="bold" /></button>
          </div>
        </form>
      )}

      {/* the only scrolling region */}
      <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto overscroll-contain">
        <AnimatePresence mode="wait" initial={false}>
          <motion.div key={tab} ref={innerRef} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.12 }} className="px-3 pb-3 space-y-2">
            {tab === "chat" ? (
              <>
                {view === "history" ? <HistoryList items={history} current={conv} onOpen={openConv}
                  onDelete={(id) => { api.deleteConversation(id).catch(() => {}); setHistory((h) => (h ?? []).filter((x) => x.conv !== id)); if (id === conv) newChat(); }} /> : <>
                {chat.length === 0 && !busy && <div className="text-[13px] text-ink-2 px-0.5 pt-0.5">{hello(user)}</div>}
                {chat.length === 0 && !busy && front.app && (
                  <div className="flex items-center gap-1.5 text-[11px] text-ink-3 px-0.5 min-w-0" title={front.title}>
                    <Eye className="size-3.5 shrink-0" weight="duotone" /><span className="shrink-0">In front:</span>
                    <span className="truncate text-ink-2">{appLabel(front.app)}{front.title ? ` · ${front.title}` : ""}</span>
                  </div>
                )}
                {chat.length === 0 && !busy && (
                  <div className="flex flex-wrap gap-1.5" data-testid="pane-chips">
                    {(chips.length ? chips : ["What's on my screen?", "What did I do in the last hour?", "Plan my day", "What is Claude doing?"]).map((q, i) => (
                      <motion.button key={q} initial={{ opacity: 0, y: 3 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.03 }}
                        className="chip hover:border-accent/50 hover:text-ink" onClick={() => ask(q)}>
                        {/screen|page|sheet|document|error/i.test(q) && <Eye className="size-3 text-accent" weight="duotone" />}{q}</motion.button>
                    ))}
                  </div>
                )}
                {chat.map((m, i) => (
                  <motion.div key={i} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} className={`flex ${m.role === "user" ? "justify-end" : ""}`}>
                    <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-[12.5px] leading-relaxed whitespace-pre-wrap break-words ${m.role === "user" ? "text-white rounded-br-md" : m.pending ? "text-ink-3 italic border border-dashed border-line rounded-bl-md" : m.error ? "bg-critical/10 border border-critical/30 rounded-bl-md" : "bg-panel-2 border border-line rounded-bl-md"}`}
                      style={m.role === "user" ? { background: "var(--accent)" } : undefined}>
                      {m.attachments?.length ? <div className={m.content ? "mb-1.5" : ""}><AttachmentChips items={m.attachments} small /></div> : null}
                      {m.screen && <ScreenPeek s={m.screen} />}
                      {m.content}<ChangeChips changes={m.changes} />
                      {m.ollamaDown && <div className="mt-1.5"><OllamaButton small onUp={() => retry(i)} /></div>}
                      {m.role === "assistant" && !m.pending && !m.error && <div className="mt-1.5 flex items-center gap-2">{m.model && <ModelTag model={m.model} latency={m.latency} />}<SpeakButton text={m.content} id={`pane-${i}`} msg={m} autoPlay={m.fresh && autoRead(readMode, m.voice)} />
                        {m.id ? <Rate value={m.rating ?? 0} onRate={(r, why) => rate(i, r, why)} /> : null}</div>}</div>
                  </motion.div>
                ))}
                {busy && <div className="flex items-center gap-2 px-2 py-2"><span className="flex gap-1"><span className="size-1.5 rounded-full bg-ink-3 live-dot" /><span className="size-1.5 rounded-full bg-ink-3 live-dot [animation-delay:.2s]" /><span className="size-1.5 rounded-full bg-ink-3 live-dot [animation-delay:.4s]" /></span>
                  <motion.span key={hint} initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-[11px] text-ink-3">{hint}</motion.span></div>}
                </>}
              </>
            ) : tab === "todos" ? (
              <PaneTodos todos={todos} onModal={setEditing} />
            ) : (
              <>
                {alerts.map((a) => (
                  <div key={a.key} className={`flex items-start gap-2.5 rounded-xl px-3.5 py-2.5 text-xs border ${a.level === "info" ? "border-line bg-panel-2/60 text-ink-2" : "border-warn/40 bg-warn/10 text-ink"}`}>
                    {a.level === "info" ? <Info className="size-4 mt-0.5 shrink-0 text-ink-3" /> : <Warning className="size-4 mt-0.5 shrink-0 text-warn" />}
                    <div className="flex-1 leading-relaxed">{a.text}</div>
                  </div>
                ))}
                {sugg.map((s) => (
                  <div key={s.id} className="rounded-xl border px-3.5 py-3" style={{ borderColor: "color-mix(in oklab, var(--accent) 35%, transparent)", background: "color-mix(in oklab, var(--accent) 8%, var(--panel))" }}>
                    <div className="flex items-start gap-2.5"><Sparkle className="size-4 mt-0.5 text-accent shrink-0" /><div className="text-[13px] leading-relaxed flex-1">{s.text}</div></div>
                    <div className="mt-2.5 flex items-center gap-1">
                      <span className="text-[10px] text-ink-3 mr-auto">{s.source === "llm" ? "model" : s.source.replace("rule:", "").replace(/_/g, " ")} · {ago(s.ts)}</span>
                      {(s.action || actionLabel(s.context))
                        ? <>
                            <button className="btn btn-primary px-3 py-1 text-xs" onClick={() => feedback(s, "accept")}>{s.action || actionLabel(s.context)}</button>
                            <button className="btn px-2.5 py-1" title="Snooze" onClick={() => feedback(s, "snooze")}><ClockCountdown className="size-4 text-warn" /></button>
                          </>
                        : <button className="btn px-2.5 py-1 text-xs" title="Good to know (leaves by itself in 15 min)" onClick={() => feedback(s, "accept")}><Check className="size-4 text-good" weight="bold" />OK</button>}
                      <button className="btn px-2.5 py-1" title="Not useful: show fewer like this" onClick={() => feedback(s, "dismiss")}><X className="size-4" weight="bold" /></button>
                    </div>
                  </div>
                ))}
                {claude.length > 0 && (
                  <div className="rounded-xl border border-line bg-panel-2/60 px-3 py-2">
                    <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-ink-3 mb-1"><Robot className="size-3.5" /> Claude Code</div>
                    {claude.slice(0, 4).map((c) => (
                      <div key={c.id} className="flex items-center gap-2 py-1 text-xs">
                        <span className={`size-1.5 rounded-full shrink-0 ${c.waiting ? "bg-warn live-dot" : c.status === "working" ? "bg-accent live-dot" : c.status === "done" ? "bg-good" : "bg-ink-3"}`} />
                        <span className="font-medium shrink-0">{c.project || c.id}</span>
                        <span className="text-ink-3 truncate">{c.status}{c.last ? ` · ${c.last}` : ""}</span>
                        <span className="ml-auto text-[10px] text-ink-3 shrink-0">{ago(c.updated)}</span>
                      </div>
                    ))}
                  </div>
                )}
                {hot.length > 0 && (
                  <div className="rounded-xl border border-line bg-panel-2/60 px-3 py-2 text-xs flex items-center gap-2"><Cpu className="size-4 text-ink-3 shrink-0" /><span className="text-ink-2">Busy:</span><span className="truncate">{hot.map(appLabel).join(", ")}</span></div>
                )}
                {count === 0 && claude.length === 0 && hot.length === 0 && <div className="text-xs text-ink-3 text-center py-6">All quiet.</div>}
              </>
            )}
          </motion.div>
        </AnimatePresence>
      </div>
    </motion.div>
  );
}

/** Open TODOs, soonest first: Overdue / Today / Later / No date, with quick add on top. Click one to edit it. */
function PaneTodos({ todos, onModal }: { todos: Todo[] | null; onModal: (open: boolean) => void }) {
  const now = Date.now() / 1000, tomorrow = addDays(new Date(), 1).getTime() / 1000;
  const open = (todos ?? []).filter((t) => t.done_ts == null);
  const groups = ([
    ["Overdue", open.filter((t) => t.due_ts != null && t.due_ts < now)],
    ["Today", open.filter((t) => t.due_ts != null && t.due_ts >= now && t.due_ts < tomorrow)],
    ["Later", open.filter((t) => t.due_ts != null && t.due_ts >= tomorrow)],
    ["No date", open.filter((t) => t.due_ts == null)],
  ] as [string, Todo[]][]).filter(([, l]) => l.length);
  return (
    <div className="space-y-2 pt-1" data-testid="pane-todos">
      <QuickAdd compact autoFocus placeholder="Add a TODO… e.g. “call the bank tomorrow 5pm !high”" />
      {todos == null ? <div className="space-y-1.5">{[0, 1, 2].map((i) => <div key={i} className="skeleton h-8" />)}</div>
        : open.length === 0 ? <div className="text-xs text-ink-3 text-center py-6">Nothing open. Add one above, or ask the chat.</div>
        : groups.map(([name, list]) => (
          <div key={name}>
            <div className={`flex items-center gap-1.5 px-0.5 pt-1 text-[10.5px] uppercase tracking-wide ${name === "Overdue" ? "text-critical" : name === "Today" ? "text-accent" : "text-ink-3"}`}>
              {name}<span className="text-ink-3">{list.length}</span>
            </div>
            <div className="-mx-1.5">{list.map((t) => <TodoRow key={t.id} t={t} compact onModal={onModal} />)}</div>
          </div>
        ))}
    </div>
  );
}

function hello(name: string) {
  const h = new Date().getHours();
  const part = h < 5 ? "Still up" : h < 12 ? "Morning" : h < 17 ? "Afternoon" : "Evening";
  return `${part}${name ? `, ${name}` : ""}. Ask me anything about your day.`;
}

function TabBtn({ active, onClick, icon, children }: { active: boolean; onClick: () => void; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <button onClick={onClick} className={`relative flex items-center gap-1.5 px-3 h-8 rounded-lg text-[12.5px] font-medium whitespace-nowrap shrink-0 transition-colors ${active ? "text-accent" : "text-ink-2 hover:text-ink hover:bg-panel-2"}`}>
      {active && <motion.span layoutId="pane-tab" className="absolute inset-0 rounded-lg" style={{ background: "color-mix(in oklab, var(--accent) 12%, transparent)" }} transition={{ type: "spring", stiffness: 500, damping: 40 }} />}
      <span className="relative flex items-center gap-1.5">{icon}{children}</span>
    </button>
  );
}

function HeadBtn({ title, onClick, children, active, testid }: { title: string; onClick: () => void; children: React.ReactNode; active?: boolean; testid?: string }) {
  return (
    <button type="button" title={title} aria-label={title} data-testid={testid} onClick={onClick}
      className={`size-8 shrink-0 grid place-items-center rounded-lg hover:bg-panel-2 ${active ? "text-accent" : "text-ink-3 hover:text-ink"}`}>{children}</button>
  );
}

/** What Strux looked at for a screen answer: a small thumbnail, larger on click. */
function ScreenPeek({ s }: { s: ScreenLook }) {
  const [big, setBig] = useState(false);
  return (
    <div className="mb-1.5">
      {s.thumb ? (
        <button type="button" onClick={() => setBig(!big)} title={big ? "Smaller" : "Bigger"} className="block rounded-lg overflow-hidden border border-line">
          <img src={`data:image/jpeg;base64,${s.thumb}`} alt="Your screen" className={`${big ? "max-h-72" : "max-h-24"} w-auto transition-[max-height] duration-200`} />
        </button>
      ) : null}
      <div className="mt-1 flex items-center gap-1 text-[10.5px] text-ink-3 min-w-0">
        <Eye className="size-3 shrink-0" weight="duotone" /><span className="truncate">{s.thumb ? "Looked at" : "From the last look at"} {appLabel(s.app || "")}{s.title ? ` · ${s.title}` : ""}</span>
      </div>
    </div>
  );
}

/** Thumbs up / down on an answer: Strux learns from it (similar questions later get the liked style, avoid the disliked one). */
function Rate({ value, onRate }: { value: number; onRate: (r: 1 | -1 | 0, why?: string) => void }) {
  const [asking, setAsking] = useState(false);
  const [why, setWhy] = useState("");
  return (
    <span className="ml-auto flex items-center gap-0.5">
      {asking ? (
        <form className="flex items-center gap-1" onSubmit={(e) => { e.preventDefault(); onRate(-1, why.trim()); setAsking(false); }}>
          <input autoFocus value={why} onChange={(e) => setWhy(e.target.value)} placeholder="What was wrong? (optional)" maxLength={300}
            onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); setAsking(false); } }}
            className="h-6 w-40 rounded-md border border-line bg-panel px-1.5 text-[11px] focus:outline-none focus:border-accent" />
        </form>
      ) : null}
      <button type="button" title="Good answer" aria-label="Good answer" onClick={() => onRate(value === 1 ? 0 : 1)}
        className={`p-0.5 rounded hover:bg-panel ${value === 1 ? "text-accent" : "text-ink-3 hover:text-ink"}`}><ThumbsUp className="size-3.5" weight={value === 1 ? "fill" : "regular"} /></button>
      <button type="button" title="Bad answer: Strux learns from it" aria-label="Bad answer" onClick={() => { if (value === -1) onRate(0); else { onRate(-1); setAsking(true); } }}
        className={`p-0.5 rounded hover:bg-panel ${value === -1 ? "text-ink" : "text-ink-3 hover:text-ink"}`}><ThumbsDown className="size-3.5" weight={value === -1 ? "fill" : "regular"} /></button>
    </span>
  );
}

function HistoryList({ items, current, onOpen, onDelete }: { items: ChatSummary[] | null; current: string; onOpen: (id: string) => void; onDelete: (id: string) => void }) {
  if (items == null) return <div className="text-xs text-ink-3 py-3 px-1">Loading…</div>;
  if (!items.length) return <div className="text-xs text-ink-3 text-center py-6">No saved chats yet.</div>;
  return (
    <div className="space-y-0.5" data-testid="pane-history-list">
      <div className="text-[10.5px] uppercase tracking-wide text-ink-3 px-1 pb-1">Recent chats</div>
      {items.map((c) => (
        <div key={c.conv} className={`group flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-panel-2 ${c.conv === current ? "bg-panel-2" : ""}`}>
          <button type="button" className="flex-1 min-w-0 text-left" onClick={() => onOpen(c.conv)}>
            <div className="text-[12.5px] truncate">{c.title}</div>
            <div className="text-[10.5px] text-ink-3">{ago(c.updated)} · {Math.ceil(c.n / 2)} {c.n > 2 ? "questions" : "question"}</div>
          </button>
          <button type="button" title="Delete this chat" aria-label="Delete this chat" onClick={() => onDelete(c.conv)}
            className="opacity-0 group-hover:opacity-100 p-1 text-ink-3 hover:text-ink"><Trash className="size-3.5" /></button>
        </div>
      ))}
    </div>
  );
}
