import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import {
  Robot, Camera, Clipboard, Cpu, Globe, Monitor, MoonStars, Play, ShieldSlash, Square, Sun, Terminal, Sparkle, Lightning, Timer, Browsers,
} from "@phosphor-icons/react";
import { useLiveCtx } from "../App";
import { api, type AppTotal, type Session, type Suggestion } from "../lib/api";
import { ago, appLabel, fmtDur, fmtTimeS } from "../lib/format";
import { AppChip, Badge, Empty, PageHeader, SectionTitle, StatTile } from "../components/Bits";
import { AppBars, HourlyArea } from "../components/Charts";
import { SuggestionCard } from "../components/Cards";
import ModelTag from "../components/ModelTag";
import { MemoryCard } from "./Memory";
import {
  EmptyAgenda, EventItem, EventModal, PlanButton, QuickAdd, TodoRow, addDays, dayStart, draftFrom, newDraft, useEvents, useTodos, type EventDraft,
} from "../components/Planner";

const ICON: Record<string, any> = {
  window: Monitor, title: Monitor, url: Globe, clipboard: Clipboard, frame: Camera, blocked: ShieldSlash, idle_start: MoonStars, idle_end: Sun,
  proc_start: Play, proc_exit: Square, proc_hot: Cpu, job_done: Terminal, claude: Robot, ocr_error: Camera,
};

function greeting() {
  const h = new Date().getHours();
  return h < 5 ? "Late night" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : h < 22 ? "Good evening" : "Late night";
}

export default function Dashboard() {
  const { status, events, suggestions: liveSugg } = useLiveCtx();
  const [apps, setApps] = useState<AppTotal[] | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [now, setNow] = useState(Date.now() / 1000);
  const [sugg, setSugg] = useState<Suggestion[]>([]);

  const load = async () => {
    try {
      const [a, t, s] = await Promise.all([api.apps(24), api.timeline(24 * 60), api.suggestions(4)]);
      setApps(a.apps); setSessions(t.sessions); setNow(t.now); setSugg(s.suggestions);
    } catch { /* server restarting: keep what is on screen, the next tick retries */ }
  };
  useEffect(() => { load(); const id = setInterval(load, 30000); return () => clearInterval(id); }, []);
  useEffect(() => { if (liveSugg.length) load(); }, [liveSugg.length]);

  const active = (apps ?? []).reduce((a, b) => a + b.seconds, 0);
  const current = sessions.length ? sessions[sessions.length - 1] : null;
  const claude = status?.claude ?? [];
  const top = status?.top_processes ?? [];
  const hot = status?.hot_processes ?? [];
  const bgEvents = useMemo(() => events.filter((e) => ["job_done", "proc_hot", "proc_start", "proc_exit"].includes(e.kind)).slice(0, 6), [events]);
  const date = new Date().toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={date}
        title={greeting()}
        subtitle={current ? <>Now in <span className="text-ink font-medium">{appLabel(current.app)}</span>{current.title ? <span className="text-ink-3"> · {current.title}</span> : null}</> : "Waiting for activity…"}
        right={<>
          {status?.paused && <Badge tone="warn">paused</Badge>}
          {status?.processing && status.processing !== "idle" && <Badge tone="accent"><Sparkle className="size-3 animate-spin" weight="duotone" />{status.processing}</Badge>}
          <span className="panel px-2.5 py-1 shadow-none" title={status?.llm ? `${status.llm.model} · runs locally` : ""}><ModelTag model={status?.llm?.model ?? "none"} className="text-[11px]" /></span>
        </>}
      />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatTile label="Active · 24h" icon={<Timer className="size-4" weight="duotone" />} value={apps ? fmtDur(active) : <span className="skeleton inline-block w-16 h-7" />} accent sub={apps ? `${apps.length} apps` : ""} />
        <StatTile label="Sessions" icon={<Browsers className="size-4" weight="duotone" />} value={sessions.length} sub="foreground switches" />
        <StatTile label="Screens kept" icon={<Camera className="size-4" weight="duotone" />} value={status?.frames ?? "—"} sub={status?.screenshot ? `${status.screenshot.deduped} deduped · ${status.screenshot.blocked} blocked · ${status.screenshot.ocr_done ?? 0} read` : ""} />
        <StatTile label="Suggestions" icon={<Lightning className="size-4" weight="duotone" />} value={status?.suggestions ?? "—"} sub={status?.brain_skip ? `brain: ${status.brain_skip}` : status?.llm_error ? "model error" : "brain idle"} />
      </div>

      <TodayRow />

      <div className="grid lg:grid-cols-5 gap-4">
        <div className="panel p-5 lg:col-span-3">
          <SectionTitle>Activity · last 24 h</SectionTitle>
          <HourlyArea sessions={sessions} now={now} />
        </div>
        <div className="panel p-5 lg:col-span-2">
          <SectionTitle>Time per app</SectionTitle>
          {apps ? <AppBars apps={apps} /> : <div className="space-y-2.5">{[0, 1, 2, 3].map((i) => <div key={i} className="skeleton h-3" />)}</div>}
        </div>
      </div>

      <div className="grid lg:grid-cols-2 gap-4">
        <motion.div layout className="panel p-5">
          <SectionTitle icon={<Robot className="size-3.5" weight="duotone" />} right={<Badge tone={claude.some((c: any) => c.waiting) ? "warn" : "default"}>{claude.length ? `${claude.length} session${claude.length > 1 ? "s" : ""}` : "no sessions"}</Badge>}>Claude Code</SectionTitle>
          {claude.length === 0 ? <Empty icon={<Robot className="size-8" weight="duotone" />}>No Claude Code activity yet. Prompts, tool calls, approvals and finishes show here.</Empty> : (
            <div className="divide-y divide-line">
              {claude.map((c: any) => (
                <div key={c.id} className="py-2.5 flex items-start gap-3">
                  <span className={`mt-1.5 size-2 rounded-full shrink-0 ${c.waiting ? "bg-warn live-dot" : c.status === "working" ? "bg-accent live-dot" : c.status === "done" ? "bg-good" : "bg-ink-3"}`} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-sm"><span className="font-medium">{c.project || c.id}</span><span className={`text-xs ${c.waiting ? "text-warn" : "text-ink-3"}`}>{c.status}</span><span className="ml-auto text-[11px] text-ink-3">{ago(c.updated)}</span></div>
                    <div className="text-xs text-ink-2 truncate mt-0.5">{c.last || "—"}</div>
                    <div className="text-[11px] text-ink-3 mt-0.5">{c.prompts} prompts · {c.tools} tool calls{c.last_tool ? ` · last ${c.last_tool}` : ""}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </motion.div>

        <motion.div layout className="panel p-5">
          <SectionTitle icon={<Cpu className="size-3.5" weight="duotone" />} right={hot.length > 0 && <Badge tone="warn">{hot.length} busy</Badge>}>Background</SectionTitle>
          {top.length === 0 ? <Empty icon={<Cpu className="size-8" weight="duotone" />}>Process snapshot arrives within 30 s.</Empty> : (
            <div className="space-y-1.5">
              {top.slice(0, 5).map((p: any) => (
                <div key={p.pid} className="grid grid-cols-[1fr_56px_72px] items-center gap-2 text-sm">
                  <span className="truncate text-ink-2">{appLabel(p.name)}{hot.includes(p.name) && <span className="ml-1.5 text-[10px] text-warn">busy</span>}</span>
                  <span className="font-mono text-xs text-right tabular-nums">{p.cpu}%</span>
                  <span className="font-mono text-xs text-right text-ink-3 tabular-nums">{p.mem_mb} MB</span>
                </div>
              ))}
            </div>
          )}
          {bgEvents.length > 0 && (
            <div className="mt-4 pt-3 border-t border-line space-y-1">
              {bgEvents.map((e, i) => {
                const Icon = ICON[e.kind] ?? Cpu;
                const txt = e.kind === "job_done" ? `${appLabel(e.app)} finished (${fmtDur(e.data.runtime_s ?? 0)})` : e.kind === "proc_hot" ? `${appLabel(e.app)} is busy (${e.data.cpu}% CPU)` : e.kind === "proc_start" ? `${appLabel(e.app)} started` : `${appLabel(e.app)} exited after ${fmtDur(e.data.runtime_s ?? 0)}`;
                return <div key={`${e.ts}-${i}`} className="flex items-center gap-2 text-xs text-ink-2"><Icon className="size-3.5 text-ink-3" weight="duotone" /><span className="truncate">{txt}</span><span className="ml-auto text-[10px] text-ink-3 font-mono">{fmtTimeS(e.ts)}</span></div>;
              })}
            </div>
          )}
        </motion.div>
      </div>

      <div className="grid lg:grid-cols-5 gap-4">
        <div className="lg:col-span-3 space-y-3">
          <SectionTitle right={<a href="#/suggestions" className="text-xs text-accent hover:underline">all</a>}>Latest suggestions</SectionTitle>
          {sugg.length ? sugg.map((s) => <SuggestionCard key={s.id} s={s} onChange={(n) => setSugg((l) => l.map((x) => (x.id === n.id ? n : x)))} />) : (
            <div className="panel"><Empty icon={<Sparkle className="size-8" weight="duotone" />}>No suggestions yet. Rules fire on their own; the model runs every few minutes.</Empty></div>
          )}
        </div>
        <div className="panel p-0 lg:col-span-2 overflow-hidden">
          <div className="px-5 pt-5"><SectionTitle>Live feed</SectionTitle></div>
          <div className="max-h-[420px] overflow-y-auto px-2 pb-2">
            {events.length === 0 && <Empty>Events appear here as they happen.</Empty>}
            {events.slice(0, 40).map((e, i) => {
              const Icon = ICON[e.kind] ?? Monitor;
              const text = e.kind === "clipboard" ? (e.data.preview ? `“${String(e.data.preview).slice(0, 60)}”` : "non-text copy")
                : e.kind === "url" ? e.data.url : e.kind === "frame" ? `screen · score ${e.data.score}` : e.kind === "idle_end" ? `back after ${fmtDur(e.data.idle_seconds ?? 0)}`
                : e.kind === "blocked" ? "blocked app, no capture" : e.kind === "claude" ? `${e.data.project || "claude"}: ${e.data.summary || e.data.event}`
                : e.kind === "job_done" ? `${appLabel(e.app)} finished` : e.kind === "proc_hot" ? `${appLabel(e.app)} busy` : e.kind.startsWith("proc_") ? `${appLabel(e.app)} ${e.kind.replace("proc_", "")}` : e.title;
              return (
                <div key={`${e.ts}-${i}`} className="flex items-start gap-3 px-3 py-2 rounded-xl hover:bg-panel-2 fade-up">
                  <Icon className="size-4 mt-0.5 text-ink-3 shrink-0" weight="duotone" />
                  <div className="min-w-0 flex-1">
                    <div className="text-xs truncate text-ink">{text || e.kind}</div>
                    <div className="text-[11px] text-ink-3 flex items-center gap-2"><AppChip app={e.app || e.kind} /><span>{ago(e.ts)}</span></div>
                  </div>
                  <span className="text-[10px] font-mono text-ink-3">{fmtTimeS(e.ts)}</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

/** Today at a glance: open TODOs (overdue / due first) with quick add + "Plan my day", and today's agenda. */
function TodayRow() {
  const { todos } = useTodos(false);
  const t0 = useMemo(() => dayStart(new Date()).getTime() / 1000, []);
  const { events } = useEvents(t0, addDays(new Date(), 2).getTime() / 1000);
  const [draft, setDraft] = useState<EventDraft | null>(null);
  const now = Date.now() / 1000;
  const today = (events ?? []).filter((e) => e.start_ts < t0 + 86400 && e.end_ts > t0);
  const upcoming = today.filter((e) => e.end_ts > now);
  const next = upcoming.length ? upcoming : (events ?? []).filter((e) => e.start_ts >= t0 + 86400).slice(0, 2);
  const open = todos ?? [];
  const overdue = open.filter((t) => t.due_ts && t.due_ts < now).length;
  return (
    <div className="grid lg:grid-cols-5 gap-4">
      <div className="panel p-5 lg:col-span-3" data-testid="dash-todos">
        <SectionTitle right={<a href="#/todos" className="text-xs text-accent hover:underline">all {open.length ? `(${open.length})` : ""}</a>}>
          TODOs{overdue ? <span className="text-critical normal-case tracking-normal font-medium ml-1">· {overdue} overdue</span> : null}
        </SectionTitle>
        <QuickAdd compact />
        <div className="-mx-2.5 mt-2">
          {todos == null ? <div className="skeleton h-16 mx-2.5" /> : open.length === 0 ? <div className="text-sm text-ink-3 py-4 text-center">No open TODOs.</div>
            : open.slice(0, 6).map((t) => <TodoRow key={t.id} t={t} compact />)}
        </div>
        {open.length > 6 && <a href="#/todos" className="block text-xs text-ink-3 hover:text-accent mt-1 px-0.5">+{open.length - 6} more</a>}
        <div className="mt-3 pt-3 border-t border-line"><PlanButton /></div>
      </div>
      <div className="lg:col-span-2 space-y-4">
      <div className="panel p-5" data-testid="dash-agenda">
        <SectionTitle right={<a href="#/calendar" className="text-xs text-accent hover:underline">calendar</a>}>
          {upcoming.length || !next.length ? "Today" : "Coming up"}
        </SectionTitle>
        {events == null ? <div className="skeleton h-16" /> : next.length ? (
          <div className="-mx-2.5 space-y-0.5">
            {next.map((e) => (
              <div key={e.id}>
                {e.start_ts >= t0 + 86400 && <div className="text-[10px] uppercase tracking-wider text-ink-3 px-2.5 mt-1">{new Date(e.start_ts * 1000).toLocaleDateString([], { weekday: "long" })}</div>}
                <EventItem e={e} onOpen={(x) => setDraft(draftFrom(x))} />
              </div>
            ))}
          </div>
        ) : <EmptyAgenda onAdd={() => setDraft(newDraft())} />}
      </div>
      <MemoryCard />
      </div>
      <EventModal draft={draft} onClose={() => setDraft(null)} />
    </div>
  );
}
