import { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise, Brain, ChatCircleDots, Eye, MagnifyingGlass, Robot, ThumbsDown, ThumbsUp } from "@phosphor-icons/react";
import { useLiveCtx, useToast } from "../App";
import { api, type KnowledgeNote, type KnowledgeStats } from "../lib/api";
import { ago } from "../lib/format";
import { SectionTitle } from "./Bits";

type Src = "claude" | "chat" | "screen";
const SRC: Record<Src, { label: string; icon: React.ReactNode; what: string }> = {
  claude: { label: "Claude Code", icon: <Robot className="size-3.5" weight="duotone" />, what: "turns read from your Claude Code sessions" },
  chat: { label: "Strux chats", icon: <ChatCircleDots className="size-3.5" weight="duotone" />, what: "questions you asked here" },
  screen: { label: "Screens", icon: <Eye className="size-3.5" weight="duotone" />, what: "times you asked about your screen" },
};

/** Memory page side panel: what Strux learns from (Claude Code, chats, screens), the learner, and the switches. */
export default function LearnedPanel() {
  const { planner } = useLiveCtx();
  const toast = useToast();
  const [k, setK] = useState<KnowledgeStats | null>(null);
  const [busy, setBusy] = useState(false);
  const [src, setSrc] = useState<Src>("claude");
  const [q, setQ] = useState("");
  const [notes, setNotes] = useState<KnowledgeNote[] | null>(null);
  const load = useCallback(() => api.knowledge().then(setK).catch(() => {}), []);
  useEffect(() => { load(); }, [load, planner]);
  useEffect(() => {
    const t = window.setTimeout(() => api.knowledgeNotes(src, q.trim(), 8).then((r) => setNotes(r.notes)).catch(() => setNotes([])), q ? 250 : 0);
    return () => window.clearTimeout(t);
  }, [src, q, k?.claude, k?.chat, k?.screen]);

  const learn = async () => {
    setBusy(true);
    try {
      const r = await api.learnNow();
      toast({ title: r.ok ? `Learned ${r.facts} things` : "Didn't learn this time", body: r.ok ? "See the Learned block." : r.status, kind: r.ok ? "good" : "warn" });
      load();
    } catch (e: any) { toast({ title: "Couldn't learn", body: String(e), kind: "critical" }); }
    finally { setBusy(false); }
  };
  const toggle = async (key: "claude" | "screens" | "recall" | "profile", on: boolean) => {
    await api.saveSettings({ learn: { [key]: on } }).catch(() => {});
    load();
  };
  const clear = async (s: Src) => {
    if (!confirm(`Forget everything Strux learned from ${SRC[s].label}? ${s === "claude" ? "(It reads the transcripts again later if Claude Code learning stays on.)" : ""}`)) return;
    const r = await api.clearKnowledge(s).catch(() => null);
    if (r) toast({ title: `Forgot ${r.deleted} notes`, body: SRC[s].label, kind: "good" });
    load();
  };
  const st = k?.learner ?? {};
  const cfg = k?.cfg ?? {};

  return (
    <aside className="panel p-4 space-y-3" data-testid="learned-panel">
      <SectionTitle icon={<Brain className="size-3.5" weight="duotone" />}
        right={<button className="btn px-2.5 py-1 text-xs" disabled={busy} onClick={learn} data-testid="learn-now">
          <ArrowsClockwise className={`size-3.5 ${busy ? "animate-spin" : ""}`} />{busy ? "Learning…" : "Learn now"}</button>}>What Strux learns</SectionTitle>
      <p className="text-xs text-ink-3 -mt-1">
        Strux reads what you ask Claude Code (and its answers), your chats here and your screen looks, and keeps a short profile in the
        <b className="text-ink-2"> Learned</b> block. Related notes and your thumbs up / down on answers shape later replies. All on this PC (data/knowledge.db).
      </p>
      <div className="grid grid-cols-3 gap-2">
        {(Object.keys(SRC) as Src[]).map((s) => (
          <button key={s} onClick={() => setSrc(s)} title={SRC[s].what}
            className={`rounded-lg border px-2 py-1.5 text-left transition-colors ${src === s ? "border-accent/50 bg-panel-2" : "border-line hover:bg-panel-2"}`}>
            <div className="flex items-center gap-1 text-[10.5px] text-ink-3 whitespace-nowrap">{SRC[s].icon}{SRC[s].label}</div>
            <div className="text-lg font-semibold tabular-nums leading-tight">{k ? k[s].toLocaleString() : "…"}</div>
          </button>
        ))}
      </div>
      <div className="text-[11px] text-ink-3 flex flex-wrap gap-x-3 gap-y-1">
        {k?.projects?.length ? <span className="truncate max-w-full">Projects: {k.projects.slice(0, 5).join(", ")}</span> : null}
        <span className="inline-flex items-center gap-1"><ThumbsUp className="size-3" />{k?.liked ?? 0}<ThumbsDown className="size-3 ml-1" />{k?.disliked ?? 0}</span>
        <span>Profile: {st.last_run ? `${st.facts ?? 0} facts, ${ago(st.last_run)}` : "not yet"}{st.status && st.status !== "ok" ? ` · ${st.status}` : ""}</span>
      </div>

      <div className="relative">
        <MagnifyingGlass className="size-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-3" />
        <input className="input py-1.5 text-[12.5px]" style={{ paddingLeft: 30 }} placeholder={`Search ${SRC[src].label.toLowerCase()}…`} value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="space-y-1.5 max-h-72 overflow-y-auto" data-testid="learned-notes">
        {notes == null ? <div className="skeleton h-16" /> : notes.length === 0 ? <div className="text-xs text-ink-3 py-2">Nothing {q ? "matches" : "yet"}.</div> : notes.map((n) => (
          <div key={n.id} className="rounded-lg border border-line px-2.5 py-1.5">
            <div className="flex items-center gap-2 text-[10.5px] text-ink-3">
              <span className="truncate">{n.project || SRC[n.source].label}</span>
              {n.rating > 0 && <ThumbsUp className="size-3 text-accent" weight="fill" />}{n.rating < 0 && <ThumbsDown className="size-3" weight="fill" />}
              <span className="ml-auto shrink-0">{ago(n.ts)}</span>
            </div>
            <div className="text-[12px] font-medium line-clamp-2">{n.title}</div>
            <div className="text-[11.5px] text-ink-2 line-clamp-2">{n.body}</div>
          </div>
        ))}
      </div>

      <div className="border-t border-line pt-2.5 space-y-1.5">
        {([["claude", "Learn from Claude Code sessions"], ["screens", "Keep notes of screen looks"], ["recall", "Use past notes in answers"], ["profile", "Keep the Learned profile up to date"]] as const).map(([key, label]) => (
          <label key={key} className="flex items-center gap-2 text-xs text-ink-2 cursor-pointer">
            <input type="checkbox" checked={cfg[key] !== false} onChange={(e) => toggle(key, e.target.checked)} />{label}
          </label>
        ))}
        <div className="flex flex-wrap gap-1.5 pt-1">
          {(Object.keys(SRC) as Src[]).map((s) => (
            <button key={s} className="text-[11px] text-ink-3 hover:text-critical underline-offset-2 hover:underline" onClick={() => clear(s)}>Forget {SRC[s].label.toLowerCase()}</button>
          ))}
        </div>
      </div>
    </aside>
  );
}
