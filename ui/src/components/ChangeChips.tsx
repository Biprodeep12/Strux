import { CalendarCheck, CheckCircle, ListPlus, Trash } from "@phosphor-icons/react";
import type { Change } from "../lib/api";

const LABEL: Record<string, string> = {
  todo_added: "Added TODO", todo_done: "Done", todo_updated: "Updated TODO", todo_deleted: "Deleted TODO",
  event_added: "Added event", event_updated: "Moved event", event_deleted: "Deleted event",
  memory_added: "Remembered", memory_removed: "Forgot",
};

/** What the chat model changed in your TODOs / calendar, under its reply. */
export default function ChangeChips({ changes }: { changes?: Change[] }) {
  if (!changes?.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5 mt-2" data-testid="change-chips">
      {changes.map((c, i) => {
        const Icon = c.kind.endsWith("deleted") ? Trash : c.kind === "todo_done" ? CheckCircle : c.kind.startsWith("event") ? CalendarCheck : ListPlus;
        const what = c.todo?.title ?? c.event?.title ?? (c as any).memory?.fact ?? (c.id != null ? `#${c.id}` : "");
        return (
          <a key={i} href={c.kind.startsWith("event") ? "#/calendar" : c.kind.startsWith("memory") ? "#/memory" : "#/todos"} target="_blank" rel="noreferrer"
            className="inline-flex items-center gap-1 rounded-md border border-accent/30 bg-accent/10 text-accent px-1.5 py-0.5 text-[11px] font-medium max-w-full">
            <Icon className="size-3 shrink-0" weight="bold" /><span className="truncate">{LABEL[c.kind] ?? c.kind}: {what}</span>
          </a>
        );
      })}
    </div>
  );
}
