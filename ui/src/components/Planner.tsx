import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowSquareOut, CalendarBlank, Check, DownloadSimple, FileText, Flag, MapPin, NotePencil, Paperclip, Plus, Robot, Sparkle, Trash, X } from "@phosphor-icons/react";
import { useLiveCtx, useToast } from "../App";
import { api, type CalEvent, type Todo, type TodoFile } from "../lib/api";
import { filesFrom, fmtSize } from "../lib/attach";
import { Modal } from "./Bits";
import ModelTag from "./ModelTag";

// ---- time helpers -------------------------------------------------------------------------------
export const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
export const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
export const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
export const hm = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export function dueLabel(ts: number | null): { text: string; tone: "overdue" | "today" | "soon" | "later" } | null {
  if (!ts) return null;
  const d = new Date(ts * 1000), now = new Date();
  const today = dayStart(now);
  const time = hm(ts);
  if (ts * 1000 < now.getTime()) return { text: sameDay(d, now) ? `Overdue · ${time}` : `Overdue · ${d.toLocaleDateString([], { day: "numeric", month: "short" })}`, tone: "overdue" };
  if (sameDay(d, now)) return { text: `Today ${time}`, tone: "today" };
  if (sameDay(d, addDays(today, 1))) return { text: `Tomorrow ${time}`, tone: "soon" };
  const days = (dayStart(d).getTime() - today.getTime()) / 86400000;
  return { text: days < 7 ? `${d.toLocaleDateString([], { weekday: "short" })} ${time}` : d.toLocaleDateString([], { day: "numeric", month: "short" }), tone: "later" };
}

/** datetime-local value <-> epoch seconds (local time). */
export const toLocalInput = (ts: number) => {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
};
export const fromLocalInput = (v: string) => (v ? new Date(v).getTime() / 1000 : null);

// ---- data hooks: reload whenever anything (you, the chat model, a suggestion) changes the planner ----
export function useTodos(includeDone = false) {
  const { planner } = useLiveCtx();
  const [todos, setTodos] = useState<Todo[] | null>(null);
  const load = useCallback(() => api.todos(includeDone).then((r) => setTodos(r.todos)).catch(() => {}), [includeDone]);
  useEffect(() => { load(); }, [load, planner]);
  return { todos, reload: load, setTodos };
}

export function useEvents(start: number, end: number) {
  const { planner } = useLiveCtx();
  const [events, setEvents] = useState<CalEvent[] | null>(null);
  const load = useCallback(() => api.events(start, end).then((r) => setEvents(r.events)).catch(() => {}), [start, end]);
  useEffect(() => { load(); }, [load, planner]);
  return { events, reload: load };
}

// ---- todos -----------------------------------------------------------------------------------
export function QuickAdd({ placeholder, autoFocus, compact }: { placeholder?: string; autoFocus?: boolean; compact?: boolean }) {
  const toast = useToast();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const add = async () => {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    try { await api.addTodo({ text: t }); setText(""); }
    catch (e: any) { toast({ title: "Couldn't add", body: String(e), kind: "critical" }); }
    finally { setBusy(false); }
  };
  return (
    <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); add(); }}>
      <input data-testid="todo-quick-add" className={`input ${compact ? "py-2 text-[13px]" : ""}`} value={text} onChange={(e) => setText(e.target.value)} autoFocus={autoFocus}
        placeholder={placeholder ?? "Add a TODO… e.g. “Fix login tomorrow 10am !high #watcher-ai”"} />
      <button className="btn btn-primary px-3 shrink-0" disabled={busy || !text.trim()} title="Add"><Plus className="size-4" weight="bold" /></button>
    </form>
  );
}

export function TodoRow({ t, compact, onModal }: { t: Todo; compact?: boolean; onModal?: (open: boolean) => void }) {
  const toast = useToast();
  const [open, setOpenRaw] = useState(false);
  const setOpen = (o: boolean) => { setOpenRaw(o); onModal?.(o); };       // the pane grows to fit the editor
  const [busy, setBusy] = useState(false);
  const done = t.done_ts != null;
  const due = dueLabel(done ? null : t.due_ts);
  const act = async (fn: () => Promise<any>) => {
    setBusy(true);
    try { await fn(); } catch (e: any) { toast({ title: "Failed", body: errText(e), kind: "critical" }); } finally { setBusy(false); }
  };
  const hasNotes = !!t.notes.trim();
  return (
    <div data-testid="todo-row" className={`group flex items-start gap-3 rounded-xl px-2.5 ${compact ? "py-1.5" : "py-2"} hover:bg-panel-2 transition-colors`}>
      <button aria-label={done ? "Mark as not done" : "Mark as done"} disabled={busy} onClick={() => act(() => api.patchTodo(t.id, { done: !done }))}
        className={`mt-0.5 size-[18px] rounded-full border-2 grid place-items-center shrink-0 transition-colors ${done ? "bg-accent border-accent text-white" : t.priority === 2 ? "border-critical/70 hover:bg-critical/10" : "border-line-2 hover:border-accent"}`}>
        {done && <Check className="size-3" weight="bold" />}
      </button>
      <button data-testid="todo-open" className="min-w-0 flex-1 text-left" onClick={() => setOpen(true)} title="Edit time, priority, notes and files">
        <div className={`text-sm leading-snug ${done ? "line-through text-ink-3" : ""}`}>{t.title}</div>
        {(due || t.project || t.priority !== 1 || t.source !== "you" || hasNotes || t.files) && (
          <div className="flex items-center gap-2 mt-0.5 flex-wrap text-[11px]">
            {due && <span className={due.tone === "overdue" ? "text-critical font-medium" : due.tone === "today" ? "text-accent font-medium" : "text-ink-3"}>{due.text}</span>}
            {t.priority === 2 && !done && <span className="inline-flex items-center gap-0.5 text-critical"><Flag className="size-3" weight="fill" />high</span>}
            {t.priority === 0 && <span className="text-ink-3">low</span>}
            {t.project && <span className="text-ink-2">#{t.project}</span>}
            {hasNotes && <span className="inline-flex items-center gap-0.5 text-ink-3 max-w-[260px] truncate" title={t.notes}><NotePencil className="size-3 shrink-0" />{t.notes.split("\n")[0]}</span>}
            {!!t.files && <span className="inline-flex items-center gap-0.5 text-ink-3"><Paperclip className="size-3" />{t.files}</span>}
            {t.source !== "you" && <span className="inline-flex items-center gap-0.5 text-accent"><Robot className="size-3" weight="duotone" />{t.source === "ai" ? "added by chat" : "from a suggestion"}</span>}
          </div>
        )}
      </button>
      <button aria-label="Delete" className="opacity-0 group-hover:opacity-100 focus:opacity-100 text-ink-3 hover:text-critical p-1 transition-opacity" disabled={busy}
        onClick={() => act(() => api.deleteTodo(t.id))}><Trash className="size-4" /></button>
      {open && <TodoModal t={t} onClose={() => setOpen(false)} />}
    </div>
  );
}

const errText = (e: unknown) => {
  const s = String(e instanceof Error ? e.message : e);
  try { return JSON.parse(s.slice(s.indexOf("{"))).detail ?? s; } catch { return s; }
};

/** Today / tomorrow at a fixed hour, or the current due moved by n days (keeps the time). */
const at = (days: number, hour: number) => { const d = addDays(new Date(), days); d.setHours(hour, 0, 0, 0); return d.getTime() / 1000; };

/** Everything about one TODO: title, due time, priority, project, notes and files. */
export function TodoModal({ t, onClose }: { t: Todo; onClose: () => void }) {
  const toast = useToast();
  const [title, setTitle] = useState(t.title);
  const [due, setDue] = useState<number | null>(t.due_ts);
  const [priority, setPriority] = useState(t.priority);
  const [project, setProject] = useState(t.project);
  const [notes, setNotes] = useState(t.notes);
  const [busy, setBusy] = useState(false);
  const done = t.done_ts != null;
  const dirty = title !== t.title || due !== t.due_ts || priority !== t.priority || project !== t.project || notes !== t.notes;

  const save = async (extra: Partial<{ done: boolean }> = {}) => {
    if (!title.trim()) return toast({ title: "Give it a title", kind: "warn" });
    setBusy(true);
    try {
      const body: Parameters<typeof api.patchTodo>[1] = { ...extra };
      if (title.trim() !== t.title) body.title = title.trim();
      if (due !== t.due_ts) body.due = due;
      if (priority !== t.priority) body.priority = priority;
      if (project !== t.project) body.project = project.trim();
      if (notes !== t.notes) body.notes = notes;
      if (Object.keys(body).length) await api.patchTodo(t.id, body);
      onClose();
    } catch (e) { toast({ title: "Couldn't save", body: errText(e), kind: "critical" }); } finally { setBusy(false); }
  };
  const close = () => { if (!dirty || confirm("Discard your changes to this TODO?")) onClose(); };
  const remove = async () => {
    if (!confirm(`Delete “${t.title}”${t.files ? ` and its ${t.files} file${t.files > 1 ? "s" : ""}` : ""}?`)) return;
    try { await api.deleteTodo(t.id); onClose(); } catch (e) { toast({ title: "Couldn't delete", body: errText(e), kind: "critical" }); }
  };
  const shift = (days: number) => setDue((d) => (d ?? at(0, 18)) + days * 86400);

  return (
    <Modal open onClose={close} title="TODO details">
      <form data-testid="todo-modal" className="space-y-4" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <input data-testid="todo-title" className="input text-[15px] font-medium" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} autoFocus />

        <div className="grid sm:grid-cols-[1fr_auto] gap-4">
          <div>
            <div className="text-xs text-ink-2 mb-1">Due</div>
            <div className="flex gap-2">
              <input data-testid="todo-due" type="datetime-local" className="input" value={due ? toLocalInput(due) : ""} onChange={(e) => setDue(fromLocalInput(e.target.value))} />
              {due != null && <button type="button" className="btn px-2.5 shrink-0" onClick={() => setDue(null)} title="No due date"><X className="size-4" /></button>}
            </div>
            <div className="flex flex-wrap gap-1.5 mt-2">
              {([["Today 18:00", () => setDue(at(0, 18))], ["Tomorrow 9:00", () => setDue(at(1, 9))], ["+1 day", () => shift(1)], ["+1 week", () => shift(7)]] as const).map(([label, fn]) => (
                <button key={label} type="button" className="px-2 py-0.5 rounded-md border border-line text-[11px] text-ink-2 hover:text-ink hover:bg-panel-2" onClick={fn}>{label}</button>
              ))}
            </div>
          </div>
          <div>
            <div className="text-xs text-ink-2 mb-1">Priority</div>
            <div data-testid="todo-priority" className="flex items-center gap-0.5 panel p-1 w-fit">
              {([[0, "Low"], [1, "Normal"], [2, "High"]] as const).map(([v, label]) => (
                <button key={v} type="button" onClick={() => setPriority(v)}
                  className={`px-3 py-1 rounded-lg text-xs font-medium transition-colors ${priority === v ? (v === 2 ? "bg-critical/15 text-critical" : "bg-accent/15 text-ink") : "text-ink-2 hover:text-ink"}`}>
                  {v === 2 && <Flag className="size-3 inline mr-1 -mt-0.5" weight={priority === 2 ? "fill" : "regular"} />}{label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <label className="block"><div className="text-xs text-ink-2 mb-1">Project</div>
          <input className="input" placeholder="optional, e.g. watcher-ai" value={project} onChange={(e) => setProject(e.target.value)} maxLength={80} />
        </label>

        <label className="block"><div className="text-xs text-ink-2 mb-1">Notes</div>
          <textarea data-testid="todo-notes" className="input min-h-28 text-[13px] leading-relaxed" placeholder="Links, steps, anything you need for this. The chat can read these."
            value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={4000} rows={Math.min(14, Math.max(4, notes.split("\n").length + 1))} />
        </label>

        <TodoFiles todoId={t.id} />

        <div className="flex items-center gap-2 pt-1 border-t border-line -mx-5 px-5 pt-4">
          <button type="button" className="btn text-critical" onClick={remove} disabled={busy}><Trash className="size-4" />Delete</button>
          <button type="button" className="btn" onClick={() => save({ done: !done })} disabled={busy}><Check className="size-4" />{done ? "Not done" : "Mark done"}</button>
          <button type="button" className="btn ml-auto" onClick={close}>Cancel</button>
          <button data-testid="todo-save" className="btn btn-primary" disabled={busy || !dirty}>Save</button>
        </div>
      </form>
    </Modal>
  );
}

/** Files kept with a TODO: upload by button, drop or paste; open in the default app, download, remove. Saved immediately. */
function TodoFiles({ todoId }: { todoId: number }) {
  const toast = useToast();
  const { planner } = useLiveCtx();
  const [files, setFiles] = useState<TodoFile[] | null>(null);
  const [uploading, setUploading] = useState(0);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const load = useCallback(() => api.todoFiles(todoId).then((r) => setFiles(r.files)).catch(() => setFiles([])), [todoId]);
  useEffect(() => { load(); }, [load, planner]);

  const upload = useCallback(async (list: File[]) => {
    if (!list.length) return;
    setUploading((n) => n + list.length);
    for (const f of list) {
      try { await api.uploadTodoFile(todoId, f); }
      catch (e) { toast({ title: `Couldn't add ${f.name || "file"}`, body: errText(e), kind: "critical" }); }
      finally { setUploading((n) => n - 1); }
    }
    load();
  }, [todoId, load, toast]);

  // paste anywhere in the TODO window (a screenshot, a copied file) adds it here
  useEffect(() => {
    const el = box.current?.closest("form");
    if (!el) return;
    const paste = (e: ClipboardEvent) => { const f = filesFrom(e.clipboardData); if (f.length) { e.preventDefault(); e.stopPropagation(); upload(f); } };
    el.addEventListener("paste", paste);
    return () => el.removeEventListener("paste", paste);
  }, [upload]);

  const open = async (f: TodoFile) => {
    try { const r = await api.openTodoFile(f.id); if (r.shown_in_folder) toast({ title: "Shown in its folder", body: "Programs and scripts are never run from Strux.", kind: "warn" }); }
    catch (e) { toast({ title: "Couldn't open", body: errText(e), kind: "critical" }); }
  };
  const remove = async (f: TodoFile) => {
    if (!confirm(`Remove “${f.name}”?`)) return;
    try { await api.deleteTodoFile(f.id); load(); } catch (e) { toast({ title: "Couldn't remove", body: errText(e), kind: "critical" }); }
  };

  return (
    <div ref={box} data-testid="todo-files">
      <div className="text-xs text-ink-2 mb-1">Files</div>
      <div onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); setOver(true); }} onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); e.stopPropagation(); setOver(false); upload(filesFrom(e.dataTransfer)); }}
        className={`rounded-xl border border-dashed p-2 transition-colors ${over ? "border-accent bg-accent/5" : "border-line-2"}`}>
        {files && files.length > 0 && (
          <div className="space-y-1 mb-2">
            {files.map((f) => (
              <div key={f.id} data-testid="todo-file" className="group/f flex items-center gap-2.5 rounded-lg px-2 py-1.5 hover:bg-panel-2">
                {f.mime.startsWith("image/")
                  ? <img src={api.todoFileUrl(f.id)} alt="" className="size-9 rounded-md object-cover border border-line shrink-0" />
                  : <span className="size-9 rounded-md bg-panel-2 border border-line grid place-items-center shrink-0"><FileText className="size-4 text-accent" weight="duotone" /></span>}
                <button type="button" className="min-w-0 flex-1 text-left" onClick={() => open(f)} title="Open in its app">
                  <div className="text-[13px] truncate">{f.name}</div>
                  <div className="text-[11px] text-ink-3">{fmtSize(f.size)} · {new Date(f.created_ts * 1000).toLocaleDateString([], { day: "numeric", month: "short" })}</div>
                </button>
                <button type="button" className="btn btn-ghost px-1.5 py-1" onClick={() => open(f)} title="Open"><ArrowSquareOut className="size-4" /></button>
                <a className="btn btn-ghost px-1.5 py-1" href={api.todoFileUrl(f.id, true)} download={f.name} title="Save a copy"><DownloadSimple className="size-4" /></a>
                <button type="button" className="btn btn-ghost px-1.5 py-1 text-ink-3 hover:text-critical" onClick={() => remove(f)} title="Remove" aria-label={`Remove ${f.name}`}><Trash className="size-4" /></button>
              </div>
            ))}
          </div>
        )}
        <div className="flex items-center gap-2 px-1 py-1 text-[12px] text-ink-3">
          <button type="button" className="btn px-2.5 py-1 text-[12px]" onClick={() => input.current?.click()}><Paperclip className="size-3.5" />Add files</button>
          <span>{uploading ? `Uploading ${uploading}…` : files == null ? "…" : "or drop / paste them here · up to 25 MB each"}</span>
          <input ref={input} type="file" multiple hidden data-testid="todo-file-input"
            onChange={(e) => { const l = Array.from(e.target.files ?? []); e.target.value = ""; upload(l); }} />
        </div>
      </div>
    </div>
  );
}

// ---- events ------------------------------------------------------------------------------------
export type EventDraft = { id?: number; title: string; start: number; end: number; all_day: boolean; location: string; notes: string };

export function newDraft(day?: Date): EventDraft {
  const now = new Date();
  const base = day && !sameDay(day, now) ? new Date(day.getFullYear(), day.getMonth(), day.getDate(), 9) : new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours() + 1);
  const s = base.getTime() / 1000;
  return { title: "", start: s, end: s + 3600, all_day: false, location: "", notes: "" };
}

export function EventModal({ draft, onClose }: { draft: EventDraft | null; onClose: () => void }) {
  const toast = useToast();
  const [d, setD] = useState<EventDraft | null>(draft);
  const [busy, setBusy] = useState(false);
  useEffect(() => setD(draft), [draft]);
  if (!d) return null;
  const set = (patch: Partial<EventDraft>) => setD({ ...d, ...patch });
  const save = async () => {
    if (!d.title.trim()) return toast({ title: "Give it a title", kind: "warn" });
    if (d.end < d.start) return toast({ title: "End is before start", kind: "warn" });
    setBusy(true);
    try {
      const body = { title: d.title.trim(), start: d.start, end: d.end, all_day: d.all_day, location: d.location, notes: d.notes };
      if (d.id) await api.patchEvent(d.id, body); else await api.addEvent(body);
      onClose();
    } catch (e: any) { toast({ title: "Couldn't save", body: String(e), kind: "critical" }); } finally { setBusy(false); }
  };
  const remove = async () => { if (!d.id) return; setBusy(true); try { await api.deleteEvent(d.id); onClose(); } finally { setBusy(false); } };
  return (
    <Modal open onClose={onClose} title={d.id ? "Edit event" : "New event"}>
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <input data-testid="event-title" className="input" autoFocus placeholder="Title" value={d.title} onChange={(e) => set({ title: e.target.value })} />
        <div className="grid grid-cols-2 gap-3">
          <label className="block"><div className="text-xs text-ink-2 mb-1">Starts</div>
            <input data-testid="event-start" type={d.all_day ? "date" : "datetime-local"} className="input" value={d.all_day ? toLocalInput(d.start).slice(0, 10) : toLocalInput(d.start)}
              onChange={(e) => { const s = fromLocalInput(d.all_day ? `${e.target.value}T00:00` : e.target.value); if (s) set({ start: s, end: Math.max(d.end - d.start + s, s) }); }} />
          </label>
          <label className="block"><div className="text-xs text-ink-2 mb-1">Ends</div>
            <input data-testid="event-end" type={d.all_day ? "date" : "datetime-local"} className="input" value={d.all_day ? toLocalInput(d.end - 1).slice(0, 10) : toLocalInput(d.end)}
              onChange={(e) => { const v = fromLocalInput(d.all_day ? `${e.target.value}T00:00` : e.target.value); if (v) set({ end: d.all_day ? v + 86400 : v }); }} />
          </label>
        </div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={d.all_day}
          onChange={(e) => { const s = new Date(d.start * 1000); const day = new Date(s.getFullYear(), s.getMonth(), s.getDate()).getTime() / 1000;
            set(e.target.checked ? { all_day: true, start: day, end: day + 86400 } : { all_day: false, start: day + 9 * 3600, end: day + 10 * 3600 }); }} /> All day</label>
        <input className="input" placeholder="Location (optional)" value={d.location} onChange={(e) => set({ location: e.target.value })} />
        <textarea className="input min-h-20" placeholder="Notes (optional)" value={d.notes} onChange={(e) => set({ notes: e.target.value })} />
        <div className="flex items-center gap-2 pt-1">
          {d.id && <button type="button" className="btn text-critical" onClick={remove} disabled={busy}><Trash className="size-4" />Delete</button>}
          <button type="button" className="btn ml-auto" onClick={onClose}>Cancel</button>
          <button data-testid="event-save" className="btn btn-primary" disabled={busy}>{d.id ? "Save" : "Add event"}</button>
        </div>
      </form>
    </Modal>
  );
}

export function EventItem({ e, onOpen }: { e: CalEvent; onOpen: (e: CalEvent) => void }) {
  const now = Date.now() / 1000;
  const live = !e.all_day && e.start_ts <= now && e.end_ts > now;
  const past = e.end_ts <= now;
  return (
    <button data-testid="event-item" onClick={() => onOpen(e)} className={`w-full text-left flex items-stretch gap-3 rounded-xl px-2.5 py-2 hover:bg-panel-2 transition-colors ${past ? "opacity-55" : ""}`}>
      <span className="w-1 rounded-full shrink-0" style={{ background: live ? "var(--accent)" : "var(--line-2)" }} />
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium truncate">{e.title}</span>
        <span className="flex items-center gap-2 text-[11px] text-ink-3">
          <span>{e.all_day ? "All day" : `${hm(e.start_ts)} – ${hm(e.end_ts)}`}</span>
          {live && <span className="text-accent font-medium">now</span>}
          {e.location && <span className="inline-flex items-center gap-0.5 truncate"><MapPin className="size-3" />{e.location}</span>}
          {e.source === "ai" && <span className="inline-flex items-center gap-0.5 text-accent"><Robot className="size-3" weight="duotone" />chat</span>}
        </span>
      </span>
    </button>
  );
}

export const draftFrom = (e: CalEvent): EventDraft =>
  ({ id: e.id, title: e.title, start: e.start_ts, end: e.end_ts, all_day: !!e.all_day, location: e.location, notes: e.notes });

// ---- AI plan -----------------------------------------------------------------------------------
export function PlanButton() {
  const [state, setState] = useState<{ busy: boolean; text?: string; error?: string; model?: string; latency?: number }>({ busy: false });
  const ref = useRef<HTMLDivElement>(null);
  const run = async () => {
    setState({ busy: true });
    try { const r = await api.plan(); setState({ busy: false, text: r.text, error: r.ok ? undefined : r.error, model: r.model, latency: r.latency }); }
    catch (e: any) { setState({ busy: false, error: String(e) }); }
    setTimeout(() => ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }), 50);
  };
  return (
    <div ref={ref}>
      <button data-testid="plan-day" className="btn" onClick={run} disabled={state.busy}>
        <Sparkle className={`size-4 text-accent ${state.busy ? "animate-spin" : ""}`} weight="duotone" />{state.busy ? "Planning…" : "Plan my day"}
      </button>
      {(state.text || state.error) && (
        <div data-testid="plan-result" className={`mt-3 rounded-xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap ${state.error ? "bg-critical/10 border border-critical/30" : "bg-panel-2 border border-line"}`}>
          {state.text || state.error}
          {state.text && <div className="mt-2"><ModelTag model={state.model} latency={state.latency} /></div>}
        </div>
      )}
    </div>
  );
}

export function EmptyAgenda({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="text-sm text-ink-3 py-6 text-center">
      <CalendarBlank className="size-7 mx-auto mb-2 text-ink-3/70" weight="duotone" />
      Nothing scheduled. <button className="text-accent hover:underline" onClick={onAdd}>Add an event</button>
    </div>
  );
}
