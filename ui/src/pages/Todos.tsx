import { useMemo, useState } from "react";
import { ChatCircleDots, CheckCircle, ListChecks } from "@phosphor-icons/react";
import type { Todo } from "../lib/api";
import { Empty, PageHeader, SectionTitle, Segmented, StatTile } from "../components/Bits";
import { PlanButton, QuickAdd, TodoRow, addDays, useTodos } from "../components/Planner";

type View = "open" | "done";

/** Buckets for open TODOs: Overdue / Today / Tomorrow / This week / Later / No date. */
function group(todos: Todo[]) {
  const now = Date.now() / 1000;
  const t1 = addDays(new Date(), 1), t2 = addDays(new Date(), 2), t7 = addDays(new Date(), 7);
  const b: Record<string, Todo[]> = { Overdue: [], Today: [], Tomorrow: [], "This week": [], Later: [], "No date": [] };
  for (const t of todos) {
    const d = t.due_ts;
    if (d == null) b["No date"].push(t);
    else if (d < now) b.Overdue.push(t);
    else if (d < t1.getTime() / 1000) b.Today.push(t);
    else if (d < t2.getTime() / 1000) b.Tomorrow.push(t);
    else if (d < t7.getTime() / 1000) b["This week"].push(t);
    else b.Later.push(t);
  }
  return Object.entries(b).filter(([, v]) => v.length);
}

export default function TodosPage() {
  const [view, setView] = useState<View>("open");
  const { todos } = useTodos(true);
  const open = useMemo(() => (todos ?? []).filter((t) => t.done_ts == null), [todos]);
  const done = useMemo(() => (todos ?? []).filter((t) => t.done_ts != null), [todos]);
  const now = Date.now() / 1000;
  const overdue = open.filter((t) => t.due_ts && t.due_ts < now).length;
  const today = open.filter((t) => t.due_ts && t.due_ts >= now && t.due_ts < addDays(new Date(), 1).getTime() / 1000).length;
  const fromAi = (todos ?? []).filter((t) => t.source !== "you").length;

  return (
    <div className="space-y-6">
      <PageHeader title="TODOs" subtitle="Yours, plus the ones the chat and suggestions add. Tell the bar chat “remind me to … tomorrow at 5” and it lands here."
        right={<><Segmented value={view} onChange={setView} options={[{ value: "open", label: `Open ${open.length}` }, { value: "done", label: `Done ${done.length}` }]} /></>} />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatTile label="Open" value={todos ? open.length : "—"} accent />
        <StatTile label="Overdue" value={todos ? overdue : "—"} sub={overdue ? "needs attention" : "all on time"} />
        <StatTile label="Due today" value={todos ? today : "—"} />
        <StatTile label="Done · 7 days" value={todos ? done.length : "—"} sub={fromAi ? `${fromAi} added by Strux` : undefined} />
      </div>

      <div className="grid lg:grid-cols-3 gap-4 items-start">
        <div className="panel p-5 lg:col-span-2 space-y-4">
          <QuickAdd autoFocus />
          {todos == null ? <div className="space-y-2">{[0, 1, 2].map((i) => <div key={i} className="skeleton h-9" />)}</div> : view === "open" ? (
            open.length === 0 ? <Empty icon={<CheckCircle className="size-8" weight="duotone" />}>Nothing open. Add one above, or ask the chat.</Empty> :
              group(open).map(([name, list]) => (
                <div key={name}>
                  <SectionTitle right={<span className="text-[11px] text-ink-3">{list.length}</span>}>
                    <span className={name === "Overdue" ? "text-critical" : name === "Today" ? "text-accent" : ""}>{name}</span>
                  </SectionTitle>
                  <div className="-mx-2.5 -mt-1">{list.map((t) => <TodoRow key={t.id} t={t} />)}</div>
                </div>
              ))
          ) : (
            done.length === 0 ? <Empty icon={<ListChecks className="size-8" weight="duotone" />}>Nothing finished in the last 7 days.</Empty> :
              <div className="-mx-2.5">{done.map((t) => <TodoRow key={t.id} t={t} />)}</div>
          )}
        </div>
        <div className="panel p-5 space-y-3 text-sm">
          <PlanButton />
          <SectionTitle icon={<ChatCircleDots className="size-3.5" weight="duotone" />}>Quick add syntax</SectionTitle>
          <ul className="space-y-1.5 text-ink-2 text-[13px]">
            <li><span className="font-mono text-ink">tomorrow 10am</span>, <span className="font-mono text-ink">fri</span>, <span className="font-mono text-ink">at 18:30</span>, <span className="font-mono text-ink">in 2 hours</span> at the end sets the due time</li>
            <li><span className="font-mono text-ink">!high</span> / <span className="font-mono text-ink">!low</span> sets priority</li>
            <li><span className="font-mono text-ink">#project</span> tags it</li>
            <li>Click a TODO to change its time, priority, notes or files</li>
          </ul>
          <div className="pt-2 border-t border-line text-[13px] text-ink-2">
            In the bar chat: “add a todo to call the bank tomorrow”, “mark the login bug done”, “what should I work on next?”.
          </div>
        </div>
      </div>
    </div>
  );
}
