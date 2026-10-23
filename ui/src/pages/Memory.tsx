import { useCallback, useEffect, useRef, useState } from "react";
import { Brain, CaretDown, Check, Eye, Plus, Robot, Trash } from "@phosphor-icons/react";
import { useLiveCtx, useToast } from "../App";
import { api, type MemoryBlock } from "../lib/api";
import { ago } from "../lib/format";
import { Badge, PageHeader, SectionTitle } from "../components/Bits";
import LearnedPanel from "../components/Learned";

/** Memory: blocks of context about you that go into every AI prompt. Edit freely; the chat adds to them too. */
export default function MemoryPage() {
  const { planner } = useLiveCtx();
  const toast = useToast();
  const [blocks, setBlocks] = useState<MemoryBlock[] | null>(null);
  const load = useCallback(() => api.memory().then((r) => setBlocks(r.blocks)).catch(() => {}), []);
  useEffect(() => { load(); }, [load, planner]);

  const add = async () => {
    try { await api.addMemory("New block", ""); load(); }
    catch (e: any) { toast({ title: "Couldn't add", body: String(e), kind: "critical" }); }
  };
  const used = (blocks ?? []).filter((b) => b.enabled && b.content.trim() && b.content.trim() !== b.hint).reduce((n, b) => n + b.content.length, 0);

  return (
    <div className="space-y-4">
      <PageHeader title="Memory" subtitle="What Strux knows about you. Every AI feature reads the switched-on blocks. Edit them here, or tell the chat “remember that …”."
        right={<button data-testid="memory-add" className="btn btn-primary" onClick={add}><Plus className="size-4" weight="bold" />New block</button>} />
      <div className="grid lg:grid-cols-[1fr_400px] gap-4 items-start">
        <div className="space-y-3">
          {blocks == null ? <div className="skeleton h-40" /> : blocks.map((b) => <BlockCard key={b.id} b={b} onChange={load} />)}
          <div className="text-[11px] text-ink-3 px-1">{used.toLocaleString()} characters in use · up to 4,000 go into each prompt · stored only on this PC (data/memory.db), kept when you clear the timeline</div>
        </div>
        <div className="space-y-4 lg:sticky lg:top-4">
          <LearnedPanel />
          <Preview />
        </div>
      </div>
    </div>
  );
}

function BlockCard({ b, onChange }: { b: MemoryBlock; onChange: () => void }) {
  const toast = useToast();
  const [title, setTitle] = useState(b.title);
  const [content, setContent] = useState(b.content);
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");
  const timer = useRef<number>();
  useEffect(() => { setTitle(b.title); setContent(b.content); }, [b.id, b.updated_ts]);   // outside edits (chat "remember")

  const save = async (patch: Partial<{ title: string; content: string; enabled: boolean }>) => {
    setState("saving");
    try { await api.patchMemory(b.id, patch); setState("saved"); window.clearTimeout(timer.current); timer.current = window.setTimeout(() => setState("idle"), 1500); }
    catch (e: any) { setState("idle"); toast({ title: "Not saved", body: String(e), kind: "critical" }); }
  };
  const remove = async () => {
    if (!confirm(`Delete the “${b.title}” block?`)) return;
    await api.deleteMemory(b.id).catch(() => {});
    onChange();
  };
  const dirty = title !== b.title || content !== b.content;

  return (
    <section data-testid="memory-block" className={`panel p-4 space-y-2 transition-opacity ${b.enabled ? "" : "opacity-60"}`}>
      <div className="flex items-center gap-2">
        <input data-testid="memory-title" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => title.trim() && title !== b.title && save({ title })}
          className="flex-1 min-w-0 bg-transparent text-sm font-semibold focus:outline-none focus:bg-panel-2 rounded px-1 -mx-1" maxLength={80} />
        {b.source === "ai" && <Badge tone="accent"><Robot className="size-3" weight="duotone" />{b.title === "Learned" ? "learned automatically" : "updated by chat"}</Badge>}
        <span className="text-[11px] text-ink-3 w-16 text-right">{state === "saving" ? "saving…" : state === "saved" ? <span className="text-good inline-flex items-center gap-0.5"><Check className="size-3" weight="bold" />saved</span> : ago(b.updated_ts)}</span>
        <label className="flex items-center gap-1.5 text-[11px] text-ink-2 cursor-pointer" title="Switched-on blocks go into every AI prompt">
          <input type="checkbox" checked={b.enabled} onChange={(e) => { save({ enabled: e.target.checked }).then(onChange); }} /> in use
        </label>
        <button aria-label={`Delete ${b.title}`} className="text-ink-3 hover:text-critical p-1" onClick={remove}><Trash className="size-4" /></button>
      </div>
      <textarea data-testid="memory-content" value={content} onChange={(e) => setContent(e.target.value)} onBlur={() => content !== b.content && save({ content })}
        placeholder={b.hint || "Anything Strux should keep in mind…"} maxLength={4000}
        className="input min-h-24 text-[13px] leading-relaxed" rows={Math.min(12, Math.max(3, content.split("\n").length + 1))} />
      {dirty && <div className="text-[11px] text-ink-3">Saves when you click outside the box.</div>}
    </section>
  );
}

function Preview() {
  const [q, setQ] = useState("What should I work on next?");
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<{ system: string; context: string; chars: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => { setBusy(true); try { setData(await api.memoryPreview(q)); setOpen(true); } finally { setBusy(false); } };
  return (
    <aside className="panel p-4 space-y-3">
      <SectionTitle icon={<Eye className="size-3.5" weight="duotone" />}>What the AI sees</SectionTitle>
      <p className="text-xs text-ink-3 -mt-1">The exact instructions, your memory and the context a chat question gets. Nothing is sent to the model here.</p>
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); run(); }}>
        <input data-testid="memory-preview-q" className="input text-[13px]" value={q} onChange={(e) => setQ(e.target.value)} />
        <button data-testid="memory-preview" className="btn shrink-0" disabled={busy}>{busy ? "…" : "Show"}</button>
      </form>
      {data && (
        <div className="space-y-2">
          <button className="w-full flex items-center gap-2 text-xs text-ink-2" onClick={() => setOpen(!open)}>
            <Brain className="size-3.5" weight="duotone" />{data.chars.toLocaleString()} characters
            <CaretDown className={`size-3 ml-auto transition-transform ${open ? "rotate-180" : ""}`} />
          </button>
          {open && (
            <div data-testid="memory-preview-out" className="max-h-[60vh] overflow-y-auto rounded-lg border border-line bg-panel-2 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words">
              <div className="text-accent font-semibold mb-1">SYSTEM</div>{data.system}
              <div className="text-accent font-semibold mt-3 mb-1">CONTEXT</div>{data.context}
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

/** Small card for the dashboard: the blocks at a glance + quick "remember". */
export function MemoryCard() {
  const { planner } = useLiveCtx();
  const toast = useToast();
  const [blocks, setBlocks] = useState<MemoryBlock[] | null>(null);
  const [fact, setFact] = useState("");
  useEffect(() => { api.memory().then((r) => setBlocks(r.blocks)).catch(() => {}); }, [planner]);
  const remember = async () => {
    const f = fact.trim();
    if (!f) return;
    try { const r = await api.remember(f); setFact(""); toast({ title: r.added ? "Remembered" : "Already in memory", body: f, kind: "good" }); }
    catch (e: any) { toast({ title: "Couldn't save", body: String(e), kind: "critical" }); }
  };
  const filled = (blocks ?? []).filter((b) => b.content.trim() && b.content.trim() !== b.hint);
  return (
    <div className="panel p-5" data-testid="dash-memory">
      <SectionTitle icon={<Brain className="size-3.5" weight="duotone" />} right={<a href="#/memory" className="text-xs text-accent hover:underline">edit</a>}>Memory</SectionTitle>
      {blocks == null ? <div className="skeleton h-16" /> : filled.length === 0 ? (
        <div className="text-sm text-ink-3 py-2">Nothing yet. Tell Strux about yourself so answers and plans fit you.</div>
      ) : (
        <div className="space-y-2">
          {filled.slice(0, 4).map((b) => (
            <a key={b.id} href="#/memory" className={`block rounded-lg px-2.5 py-1.5 hover:bg-panel-2 ${b.enabled ? "" : "opacity-50"}`}>
              <div className="text-[11px] font-semibold text-ink-2">{b.title}</div>
              <div className="text-xs text-ink-3 truncate">{b.content.split("\n").filter(Boolean).slice(-1)[0]}</div>
            </a>
          ))}
        </div>
      )}
      <form className="flex gap-2 mt-3" onSubmit={(e) => { e.preventDefault(); remember(); }}>
        <input data-testid="dash-remember" className="input py-2 text-[13px]" placeholder="Remember that…" value={fact} onChange={(e) => setFact(e.target.value)} maxLength={400} />
        <button className="btn px-3" disabled={!fact.trim()} title="Add to Notes"><Plus className="size-4" weight="bold" /></button>
      </form>
    </div>
  );
}
