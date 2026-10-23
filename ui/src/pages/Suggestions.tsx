import { useEffect, useMemo, useState } from "react";
import { Sparkle } from "@phosphor-icons/react";
import { useLiveCtx } from "../App";
import { api, type Suggestion } from "../lib/api";
import { Empty, PageHeader, Segmented, StatTile } from "../components/Bits";
import { SuggestionCard } from "../components/Cards";

type Filter = "all" | "open" | "accept" | "dismiss" | "snooze";

export default function Suggestions() {
  const { suggestions: live } = useLiveCtx();
  const [items, setItems] = useState<Suggestion[]>([]);
  const [filter, setFilter] = useState<Filter>("all");

  const load = async () => setItems((await api.suggestions(200)).suggestions);
  useEffect(() => { load(); }, [live.length]);

  const stats = useMemo(() => {
    const c = { total: items.length, accept: 0, dismiss: 0, snooze: 0, open: 0, llm: 0 };
    for (const s of items) {
      if (s.feedback === "accept") c.accept++; else if (s.feedback === "dismiss") c.dismiss++; else if (s.feedback === "snooze") c.snooze++; else c.open++;
      if (s.source === "llm") c.llm++;
    }
    return c;
  }, [items]);

  const shown = items.filter((s) => filter === "all" ? true : filter === "open" ? !s.feedback : s.feedback === filter);
  const rate = stats.accept + stats.dismiss > 0 ? Math.round((stats.accept / (stats.accept + stats.dismiss)) * 100) : null;

  return (
    <div className="space-y-6">
      <PageHeader title="Suggestions" subtitle="Accepting one pins the screens around it and raises their retention score. Dismissals teach the model what not to repeat."
        right={<Segmented value={filter} onChange={setFilter} options={[{ value: "all", label: "All" }, { value: "open", label: "Open" }, { value: "accept", label: "Accepted" }, { value: "snooze", label: "Snoozed" }, { value: "dismiss", label: "Dismissed" }]} />} />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatTile label="Total" value={stats.total} sub={`${stats.llm} from the model`} />
        <StatTile label="Open" value={stats.open} accent />
        <StatTile label="Accepted" value={stats.accept} />
        <StatTile label="Accept rate" value={rate == null ? "—" : `${rate}%`} sub="of decided" />
      </div>

      <div className="space-y-3 max-w-3xl">
        {shown.length === 0 && <div className="panel"><Empty icon={<Sparkle className="size-8" weight="duotone" />}>Nothing here yet.</Empty></div>}
        {shown.map((s) => <SuggestionCard key={s.id} s={s} onChange={(n) => setItems((l) => l.map((x) => (x.id === n.id ? n : x)))} />)}
      </div>
    </div>
  );
}
