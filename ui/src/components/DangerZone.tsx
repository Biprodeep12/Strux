import { useState } from "react";
import { Trash, WarningCircle } from "@phosphor-icons/react";
import { useToast } from "../App";
import { api } from "../lib/api";
import { SectionTitle } from "./Bits";

/** Settings → Clear timeline: the whole activity history, after typing DELETE. */
export default function DangerZone() {
  const toast = useToast();
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const clear = async () => {
    setBusy(true);
    try {
      const r = await api.clearTimeline();
      setTyped("");
      toast({ title: "Timeline cleared", body: `${r.events.toLocaleString()} events, ${r.frames.toLocaleString()} screenshots, ${r.files.toLocaleString()} image files deleted.`, kind: "good", ttl: 8000 });
    } catch (e: any) { toast({ title: "Couldn't clear", body: String(e), kind: "critical" }); }
    finally { setBusy(false); }
  };
  return (
    <section className="panel p-5 space-y-3 border-critical/30" data-testid="danger-zone">
      <SectionTitle icon={<WarningCircle className="size-3.5 text-critical" weight="duotone" />}>Clear timeline</SectionTitle>
      <p className="text-sm text-ink-2">
        Deletes your whole activity history: every window / app event, every screenshot (and its image file), the search
        index and the hourly / daily summaries. It can't be undone. Your TODOs, calendar, Memory, suggestions and settings stay.
      </p>
      <div className="flex gap-2 max-w-md">
        <input data-testid="danger-confirm" className="input" placeholder="Type DELETE to confirm" value={typed} onChange={(e) => setTyped(e.target.value)} />
        <button data-testid="danger-clear" className="btn shrink-0 text-white border-transparent disabled:opacity-40" style={{ background: "var(--critical)" }}
          disabled={typed !== "DELETE" || busy} onClick={clear}>
          <Trash className="size-4" />{busy ? "Clearing…" : "Clear timeline"}
        </button>
      </div>
    </section>
  );
}
