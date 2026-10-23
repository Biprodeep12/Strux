import { useEffect, useRef, useState } from "react";
import { Camera, Clipboard, Globe, MagnifyingGlass, Monitor } from "@phosphor-icons/react";
import { api, type Hit } from "../lib/api";
import { fmtDate, fmtTime } from "../lib/format";
import { AppChip, Badge, Empty, PageHeader } from "../components/Bits";

const ICON: Record<string, any> = { window: Monitor, url: Globe, clipboard: Clipboard, frame: Camera };

function Snippet({ text }: { text: string }) {
  const parts = text.split(/(\[[^\]]+\])/g);
  return <span>{parts.map((p, i) => p.startsWith("[") && p.endsWith("]") ? <mark key={i} className="rounded px-0.5 text-ink" style={{ background: "color-mix(in oklab, var(--accent) 25%, transparent)" }}>{p.slice(1, -1)}</mark> : <span key={i}>{p}</span>)}</span>;
}

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const t = useRef<number | undefined>(undefined);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => {
    window.clearTimeout(t.current);
    if (!q.trim()) { setHits(null); return; }
    t.current = window.setTimeout(async () => { setBusy(true); try { setHits((await api.search(q)).hits); } finally { setBusy(false); } }, 220);
  }, [q]);

  return (
    <div className="space-y-6">
      <PageHeader title="Search" subtitle="Full-text over window titles, URLs, clipboard previews and screen text. Local only." />

      <div className="relative max-w-2xl">
        <MagnifyingGlass className="size-4 text-ink-3 absolute left-3.5 top-1/2 -translate-y-1/2" weight="bold" />
        <input ref={inputRef} value={q} onChange={(e) => setQ(e.target.value)} placeholder="traceback, pull request, invoice, figma…" className="input pl-10 py-3 text-base" />
        {busy && <span className="absolute right-3.5 top-1/2 -translate-y-1/2 size-2 rounded-full bg-accent live-dot" />}
      </div>

      <div className="max-w-3xl space-y-2">
        {hits === null && <div className="text-sm text-ink-3">Type to search. Screen text is included when OCR is on.</div>}
        {hits && hits.length === 0 && <div className="panel"><Empty icon={<MagnifyingGlass className="size-8" weight="duotone" />}>No matches.</Empty></div>}
        {hits?.map((h, i) => {
          const Icon = ICON[h.kind] ?? Monitor;
          return (
            <div key={`${h.kind}-${h.ref_id}-${i}`} className="panel px-4 py-3 flex items-start gap-3 fade-up">
              <div className="size-9 rounded-xl bg-panel-2 grid place-items-center shrink-0 text-ink-2"><Icon className="size-4" weight="duotone" /></div>
              <div className="min-w-0 flex-1">
                <div className="text-sm truncate">{h.title || <span className="text-ink-3">(no title)</span>}</div>
                {h.snip && <div className="text-xs text-ink-2 mt-0.5 leading-relaxed"><Snippet text={h.snip} /></div>}
                <div className="mt-1.5 flex items-center gap-2 text-[11px] text-ink-3">
                  <Badge>{h.kind}</Badge><AppChip app={h.app} /><span>{fmtDate(h.ts)} {fmtTime(h.ts)}</span>
                  {h.kind === "frame" && <a className="text-accent hover:underline" href={`/api/frames/${h.ref_id}/image`} target="_blank" rel="noreferrer">open image</a>}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
