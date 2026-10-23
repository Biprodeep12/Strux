import { useEffect, useMemo, useState } from "react";
import { Camera, Sparkle } from "@phosphor-icons/react";
import { api, type Frame, type Marker, type Session } from "../lib/api";
import { appColor, appLabel, fmtDur, fmtTime } from "../lib/format";
import { AppChip, Badge, Empty, Modal, PageHeader, SectionTitle, Segmented } from "../components/Bits";
import { FrameCard } from "../components/Cards";
import ModelTag from "../components/ModelTag";

const RANGES = [{ value: "60", label: "1h" }, { value: "180", label: "3h" }, { value: "480", label: "8h" }, { value: "1440", label: "24h" }];

export default function Timeline() {
  const [range, setRange] = useState("180");
  const minutes = Number(range);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [markers, setMarkers] = useState<Marker[]>([]);
  const [frames, setFrames] = useState<Frame[]>([]);
  const [open, setOpen] = useState<Frame | null>(null);
  const [explain, setExplain] = useState<{ busy: boolean; text?: string; error?: string; model?: string; latency?: number }>({ busy: false });
  const [appFilter, setAppFilter] = useState<string | null>(null);

  const load = async () => {
    try {
      const [t, f] = await Promise.all([api.timeline(minutes), api.frames(minutes, 240)]);
      setSessions(t.sessions.slice().reverse()); setMarkers(t.markers);
      // keep the same array when nothing changed, so 200+ thumbnails don't re-render every refresh
      setFrames((old) => (old.length === f.frames.length && old[0]?.id === f.frames[0]?.id ? old : f.frames));
    } catch { /* server restarting: keep what is on screen, the next tick retries */ }
  };
  useEffect(() => { load(); const id = setInterval(load, 20000); return () => clearInterval(id); }, [minutes]);

  const apps = useMemo(() => Array.from(new Set(sessions.map((s) => s.app))), [sessions]);
  const total = sessions.reduce((a, s) => a + s.seconds, 0);
  const shown = appFilter ? sessions.filter((s) => s.app === appFilter) : sessions;
  const shownFrames = appFilter ? frames.filter((f) => f.app === appFilter) : frames;

  const doExplain = async (f: Frame) => {
    setExplain({ busy: true });
    try { const r = await api.explain(f.id); setExplain({ busy: false, text: r.text, error: r.ok ? undefined : r.error, model: r.model, latency: r.latency }); }
    catch (e: any) { setExplain({ busy: false, error: String(e) }); }
  };

  return (
    <div className="space-y-6">
      <PageHeader title="Timeline" subtitle={`${fmtDur(total)} active across ${sessions.length} sessions`} right={<Segmented value={range} options={RANGES} onChange={setRange} />} />

      {sessions.length > 0 && (
        <div className="panel p-4">
          <div className="flex h-3 rounded-full overflow-hidden gap-px bg-bg">
            {sessions.map((s, i) => (
              <div key={i} title={`${appLabel(s.app)} · ${fmtTime(s.start)}–${fmtTime(s.end)} · ${fmtDur(s.seconds)}`}
                className="h-full transition-opacity hover:opacity-80" style={{ width: `${Math.max(0.4, (s.seconds / total) * 100)}%`, background: appColor(s.app), opacity: appFilter && appFilter !== s.app ? 0.25 : 1 }} />
            ))}
          </div>
          <div className="mt-3 flex items-center gap-2 flex-wrap">
            <button onClick={() => setAppFilter(null)} className={`chip ${!appFilter ? "border-accent/40 text-ink" : ""}`}>All</button>
            {apps.map((a) => (
              <button key={a} onClick={() => setAppFilter(appFilter === a ? null : a)} className={`chip ${appFilter === a ? "border-accent/40 text-ink" : ""}`}>
                <span className="size-2 rounded-full" style={{ background: appColor(a) }} />{appLabel(a)}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="grid lg:grid-cols-5 gap-4">
        <div className="lg:col-span-2 panel p-0 overflow-hidden">
          <div className="px-5 pt-5"><SectionTitle>Sessions</SectionTitle></div>
          <div className="max-h-[640px] overflow-y-auto px-2 pb-2">
            {shown.length === 0 && <Empty>No sessions in this range.</Empty>}
            {shown.map((s, i) => {
              const ms = markers.filter((m) => m.ts >= s.start && m.ts <= s.end && (m.kind === "url" || m.kind === "clipboard" || m.kind === "blocked"));
              return (
                <div key={i} className="px-3 py-2.5 rounded-xl hover:bg-panel-2 fade-up">
                  <div className="flex items-center justify-between gap-3">
                    <AppChip app={s.app} size="md" />
                    <span className="text-[11px] font-mono text-ink-3">{fmtTime(s.start)} · {fmtDur(s.seconds)}</span>
                  </div>
                  <div className="text-xs text-ink-2 truncate mt-0.5" title={s.title}>{s.title || "—"}</div>
                  {ms.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {ms.slice(0, 4).map((m, j) => (
                        <span key={j} className="text-[10px] text-ink-3 truncate max-w-[220px] px-1.5 py-0.5 rounded-md bg-bg border border-line" title={m.text}>
                          {m.kind === "url" ? m.text.replace(/^https?:\/\//, "") : m.kind === "blocked" ? "blocked" : `copy: ${m.text}`}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div className="lg:col-span-3">
          <SectionTitle icon={<Camera className="size-3.5" weight="duotone" />} right={<span className="text-xs text-ink-3">{shownFrames.length} frames</span>}>Screens</SectionTitle>
          {shownFrames.length === 0 ? <div className="panel"><Empty icon={<Camera className="size-8" weight="duotone" />}>No frames in this range.</Empty></div> : (
            <div className="grid grid-cols-2 xl:grid-cols-3 gap-3">
              {shownFrames.map((f) => <FrameCard key={f.id} f={f} onOpen={(x) => { setOpen(x); setExplain({ busy: false }); }} />)}
            </div>
          )}
        </div>
      </div>

      <Modal open={!!open} onClose={() => setOpen(null)} wide title={open && <span className="flex items-center gap-2"><AppChip app={open.app} /> <span className="text-ink-3">{fmtTime(open.ts)}</span> <span className="truncate">{open.title}</span></span>}>
        {open && (
          <div className="space-y-4">
            {open.image_path ? <img src={`/api/frames/${open.id}/image`} alt="" className="w-full rounded-xl border border-line" />
              : <div className="rounded-xl border border-line bg-panel-2 h-40 grid place-items-center text-ink-3 text-sm">Pixels were removed by retention ({open.tier}).</div>}
            <div className="flex items-center gap-2 flex-wrap">
              <Badge tone="accent">score {open.score}</Badge><Badge>{open.tier}</Badge>{!!open.pinned && <Badge tone="good">pinned</Badge>}
              <button className="btn btn-primary ml-auto" disabled={explain.busy || !open.image_path} onClick={() => doExplain(open)}>
                <Sparkle className="size-4" weight="duotone" />{explain.busy ? "Looking…" : "Explain this screen"}
              </button>
            </div>
            {explain.error && <div className="text-sm text-critical bg-critical/10 border border-critical/30 rounded-xl px-3 py-2">{explain.error}</div>}
            {explain.text && <div className="text-sm leading-relaxed bg-panel-2 border border-line rounded-xl px-4 py-3 whitespace-pre-wrap">{explain.text}<div className="mt-2"><ModelTag model={explain.model} latency={explain.latency} /></div></div>}
          </div>
        )}
      </Modal>
    </div>
  );
}
