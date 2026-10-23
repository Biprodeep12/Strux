import { useState } from "react";
import { motion } from "framer-motion";
import { Check, ClockCountdown, ImageBroken, PushPin, Sparkle, X } from "@phosphor-icons/react";
import { api, type Frame, type Suggestion } from "../lib/api";
import { actionLabel, ago, appLabel, fmtTime } from "../lib/format";
import { AppChip, Badge } from "./Bits";
import { useLiveCtx } from "../App";
import { modelLabel } from "../lib/format";

export function SuggestionCard({ s, onChange, compact }: { s: Suggestion; onChange?: (s: Suggestion) => void; compact?: boolean }) {
  const [busy, setBusy] = useState(false);
  const act = async (value: "accept" | "dismiss" | "snooze") => {
    setBusy(true);
    try { await api.feedback(s.id, value); onChange?.({ ...s, feedback: value }); } finally { setBusy(false); }
  };
  const isLlm = s.source === "llm";
  const { status } = useLiveCtx();
  const action = actionLabel(s.context);
  return (
    <motion.div layout initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} className="panel p-4">
      <div className="flex items-start gap-3">
        <div className={`mt-0.5 size-8 rounded-xl grid place-items-center shrink-0 ${isLlm ? "text-accent" : "text-ink-2"}`} style={{ background: isLlm ? "color-mix(in oklab, var(--accent) 12%, transparent)" : "var(--panel-2)" }}>
          <Sparkle className="size-4" weight="duotone" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm leading-relaxed">{s.text}</div>
          <div className="mt-2 flex items-center gap-2 flex-wrap">
            <Badge tone={isLlm ? "accent" : "default"}>{isLlm ? modelLabel(status?.llm?.model) : s.source.replace("rule:", "").replace(/_/g, " ")}</Badge>
            <span className="text-[11px] text-ink-3">{ago(s.ts)}</span>
            {s.feedback && <Badge tone={s.feedback === "accept" ? "good" : s.feedback === "snooze" ? "warn" : "default"}>{s.feedback}</Badge>}
          </div>
        </div>
        {!s.feedback && !compact && (
          <div className="flex items-center gap-1 shrink-0">
            {action
              ? <button className="btn btn-primary px-3 text-xs" title="Accept: Strux does it and answers in the bar's chat" disabled={busy} onClick={() => act("accept")}>{action}</button>
              : <button className="btn px-2" title="Accept (pins the frames around it)" disabled={busy} onClick={() => act("accept")}><Check className="size-4 text-good" weight="bold" /></button>}
            <button className="btn px-2" title="Snooze" disabled={busy} onClick={() => act("snooze")}><ClockCountdown className="size-4 text-warn" weight="duotone" /></button>
            <button className="btn px-2" title="Dismiss" disabled={busy} onClick={() => act("dismiss")}><X className="size-4 text-ink-2" weight="bold" /></button>
          </div>
        )}
      </div>
    </motion.div>
  );
}

export function FrameCard({ f, onOpen }: { f: Frame; onOpen: (f: Frame) => void }) {
  const [broken, setBroken] = useState(false);
  const hasImg = !!f.image_path && !broken;
  return (
    <button onClick={() => onOpen(f)} className="group text-left panel overflow-hidden transition-transform duration-200 hover:-translate-y-0.5" style={{ contentVisibility: "auto", containIntrinsicSize: "auto 220px" }}>
      <div className="aspect-video bg-panel-2 relative overflow-hidden">
        {hasImg ? (
          <img src={`/api/frames/${f.id}/image`} alt="" loading="lazy" decoding="async" onError={() => setBroken(true)} className="w-full h-full object-cover group-hover:scale-[1.03] transition-transform duration-300" />
        ) : (
          <div className="absolute inset-0 grid place-items-center text-ink-3"><ImageBroken className="size-6" weight="duotone" /></div>
        )}
        <div className="absolute top-2 left-2 flex gap-1">
          <Badge tone={f.score >= 60 ? "accent" : "default"}>{f.score}</Badge>
          <Badge>{f.tier}</Badge>
          {!!f.pinned && <Badge tone="good"><PushPin className="size-3" weight="fill" /></Badge>}
        </div>
      </div>
      <div className="px-3 py-2">
        <div className="flex items-center justify-between gap-2">
          <AppChip app={f.app} />
          <span className="text-[11px] text-ink-3 font-mono">{fmtTime(f.ts)}</span>
        </div>
        <div className="text-xs text-ink-2 truncate mt-0.5" title={f.title}>{f.title || appLabel(f.app)}</div>
      </div>
    </button>
  );
}
