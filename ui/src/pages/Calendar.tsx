import { useMemo, useState } from "react";
import { CaretLeft, CaretRight, Plus } from "@phosphor-icons/react";
import type { CalEvent, Todo } from "../lib/api";
import { PageHeader, SectionTitle } from "../components/Bits";
import {
  EmptyAgenda, EventItem, EventModal, PlanButton, TodoRow, addDays, dayStart, draftFrom, hm, newDraft, sameDay, useEvents, useTodos,
  type EventDraft,
} from "../components/Planner";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Monday-first 6-week grid around `month`. */
function gridDays(month: Date): Date[] {
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const lead = (first.getDay() + 6) % 7;
  const start = addDays(first, -lead);
  return Array.from({ length: 42 }, (_, i) => addDays(start, i));
}

const onDay = (e: CalEvent, d: Date) => {
  const a = dayStart(d).getTime() / 1000, b = addDays(d, 1).getTime() / 1000;
  return e.start_ts < b && e.end_ts > a;
};

export default function CalendarPage() {
  const [month, setMonth] = useState(() => new Date(new Date().getFullYear(), new Date().getMonth(), 1));
  const [sel, setSel] = useState(() => dayStart(new Date()));
  const [draft, setDraft] = useState<EventDraft | null>(null);
  const days = useMemo(() => gridDays(month), [month]);
  const range = useMemo(() => [days[0].getTime() / 1000, addDays(days[41], 1).getTime() / 1000] as const, [days]);
  const { events } = useEvents(range[0], range[1]);
  const { todos } = useTodos(false);
  const today = new Date();

  const dueOn = (d: Date): Todo[] => (todos ?? []).filter((t) => t.due_ts && sameDay(new Date(t.due_ts * 1000), d));
  const selEvents = (events ?? []).filter((e) => onDay(e, sel)).sort((a, b) => b.all_day - a.all_day || a.start_ts - b.start_ts);
  const selTodos = dueOn(sel);
  const go = (n: number) => setMonth(new Date(month.getFullYear(), month.getMonth() + n, 1));

  return (
    <div className="space-y-6">
      <PageHeader title="Calendar" subtitle="Your events and TODO due dates. The chat can add, move and read these (“schedule a call with Raj tomorrow 3pm”)."
        right={<button data-testid="new-event" className="btn btn-primary" onClick={() => setDraft(newDraft(sel))}><Plus className="size-4" weight="bold" />New event</button>} />

      <div className="grid lg:grid-cols-[1fr_340px] gap-4 items-start">
        <div className="panel p-4">
          <div className="flex items-center gap-2 mb-3">
            <div className="text-lg font-semibold tracking-tight" data-testid="cal-month">{month.toLocaleDateString([], { month: "long", year: "numeric" })}</div>
            <div className="ml-auto flex items-center gap-1">
              <button className="btn px-2" aria-label="Previous month" onClick={() => go(-1)}><CaretLeft className="size-4" /></button>
              <button className="btn" onClick={() => { const n = new Date(); setMonth(new Date(n.getFullYear(), n.getMonth(), 1)); setSel(dayStart(n)); }}>Today</button>
              <button className="btn px-2" aria-label="Next month" onClick={() => go(1)}><CaretRight className="size-4" /></button>
            </div>
          </div>
          <div className="grid grid-cols-7 text-[11px] text-ink-3 uppercase tracking-wider mb-1">
            {WEEKDAYS.map((w) => <div key={w} className="px-2 py-1">{w}</div>)}
          </div>
          <div className="grid grid-cols-7 gap-px rounded-xl overflow-hidden border border-line" style={{ background: "var(--line)" }}>
            {days.map((d) => {
              const inMonth = d.getMonth() === month.getMonth();
              const isToday = sameDay(d, today), isSel = sameDay(d, sel);
              const evs = (events ?? []).filter((e) => onDay(e, d));
              const due = dueOn(d);
              return (
                <button key={d.toISOString()} data-testid="cal-day" data-date={`${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`}
                  onClick={() => setSel(dayStart(d))} onDoubleClick={() => setDraft(newDraft(d))}
                  className={`min-h-[92px] text-left p-1.5 flex flex-col gap-1 transition-colors ${isSel ? "bg-accent/8" : "bg-panel hover:bg-panel-2"}`}>
                  <span className={`text-xs w-6 h-6 grid place-items-center rounded-full ${isToday ? "bg-accent text-white font-semibold" : inMonth ? "text-ink" : "text-ink-3"}`}>{d.getDate()}</span>
                  {evs.slice(0, 3).map((e) => (
                    <span key={e.id} className="text-[11px] leading-tight truncate rounded px-1 py-0.5"
                      style={{ background: "color-mix(in oklab, var(--accent) 14%, transparent)", color: "var(--accent-2)" }}>
                      {e.all_day ? "" : `${hm(e.start_ts)} `}{e.title}
                    </span>
                  ))}
                  {evs.length > 3 && <span className="text-[10px] text-ink-3 px-1">+{evs.length - 3} more</span>}
                  {due.length > 0 && <span className="text-[10px] text-ink-2 px-1 truncate">☐ {due.length === 1 ? due[0].title : `${due.length} TODOs due`}</span>}
                </button>
              );
            })}
          </div>
          <div className="text-[11px] text-ink-3 mt-2">Double-click a day to add an event there.</div>
        </div>

        <div className="space-y-4">
          <div className="panel p-4">
            <SectionTitle right={<button className="text-xs text-accent hover:underline" onClick={() => setDraft(newDraft(sel))}>+ event</button>}>
              {sameDay(sel, today) ? "Today" : sel.toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" })}
            </SectionTitle>
            {events == null ? <div className="skeleton h-16" /> : selEvents.length ? (
              <div className="-mx-2.5 space-y-0.5">{selEvents.map((e) => <EventItem key={e.id} e={e} onOpen={(x) => setDraft(draftFrom(x))} />)}</div>
            ) : <EmptyAgenda onAdd={() => setDraft(newDraft(sel))} />}
            {selTodos.length > 0 && (
              <div className="mt-3 pt-3 border-t border-line">
                <SectionTitle>Due this day</SectionTitle>
                <div className="-mx-2.5">{selTodos.map((t) => <TodoRow key={t.id} t={t} compact />)}</div>
              </div>
            )}
          </div>
          <div className="panel p-4"><PlanButton /></div>
        </div>
      </div>

      <EventModal draft={draft} onClose={() => setDraft(null)} />
    </div>
  );
}
