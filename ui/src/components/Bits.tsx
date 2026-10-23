import type { ReactNode } from "react";
import { motion } from "framer-motion";
import { X } from "@phosphor-icons/react";
import { appColor, appLabel } from "../lib/format";

export function PageHeader({ eyebrow, title, subtitle, right }: { eyebrow?: ReactNode; title: ReactNode; subtitle?: ReactNode; right?: ReactNode }) {
  return (
    <header className="flex items-end justify-between gap-4 mb-6">
      <div className="min-w-0">
        {eyebrow && <div className="text-xs text-ink-3 mb-1">{eyebrow}</div>}
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="text-sm text-ink-2 mt-1 truncate max-w-[680px]">{subtitle}</p>}
      </div>
      {right && <div className="flex items-center gap-2 shrink-0">{right}</div>}
    </header>
  );
}

export function StatTile({ label, value, sub, accent, icon }: { label: string; value: ReactNode; sub?: ReactNode; accent?: boolean; icon?: ReactNode }) {
  return (
    <motion.div whileHover={{ y: -2 }} transition={{ type: "spring", stiffness: 400, damping: 30 }} className="panel px-5 py-4 fade-up">
      <div className="flex items-center justify-between">
        <div className="text-[11px] text-ink-3 uppercase tracking-wider">{label}</div>
        {icon && <div className="text-ink-3">{icon}</div>}
      </div>
      <div className={`mt-1.5 text-[26px] leading-none font-semibold tabular-nums tracking-tight ${accent ? "text-accent" : ""}`}>{value}</div>
      {sub && <div className="text-xs text-ink-2 mt-2">{sub}</div>}
    </motion.div>
  );
}

export function SectionTitle({ children, right, icon }: { children: ReactNode; right?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-3">
      <h2 className="text-[11px] font-semibold text-ink-3 uppercase tracking-wider inline-flex items-center gap-1.5">{icon}{children}</h2>
      {right}
    </div>
  );
}

export function AppChip({ app, size = "sm" }: { app: string; size?: "sm" | "md" }) {
  return (
    <span className={`inline-flex items-center gap-1.5 ${size === "md" ? "text-sm" : "text-xs"} text-ink-2`}>
      <span className="size-2 rounded-full shrink-0" style={{ background: appColor(app) }} />
      {appLabel(app)}
    </span>
  );
}

export function Empty({ children, icon }: { children: ReactNode; icon?: ReactNode }) {
  return (
    <div className="text-sm text-ink-3 py-10 text-center flex flex-col items-center gap-2">
      {icon && <div className="text-ink-3/70">{icon}</div>}
      <div className="max-w-[320px] leading-relaxed">{children}</div>
    </div>
  );
}

export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title?: ReactNode; children: ReactNode; wide?: boolean }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm grid place-items-center p-6" onClick={onClose}>
      <motion.div initial={{ opacity: 0, scale: 0.97, y: 8 }} animate={{ opacity: 1, scale: 1, y: 0 }} transition={{ type: "spring", stiffness: 380, damping: 30 }}
        className={`panel w-full ${wide ? "max-w-5xl" : "max-w-2xl"} max-h-[90vh] overflow-hidden flex flex-col`} style={{ boxShadow: "var(--shadow-lg)" }} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b border-line">
          <div className="text-sm font-medium truncate pr-4">{title}</div>
          <button className="btn btn-ghost px-1.5 py-1" onClick={onClose}><X className="size-4" weight="bold" /></button>
        </div>
        <div className="overflow-y-auto p-5">{children}</div>
      </motion.div>
    </div>
  );
}

export function Badge({ children, tone = "default" }: { children: ReactNode; tone?: "default" | "accent" | "good" | "warn" | "critical" }) {
  const t = {
    default: "bg-panel-2 text-ink-2 border-line",
    accent: "bg-accent/12 text-accent border-accent/25",
    good: "bg-good/12 text-good border-good/25",
    warn: "bg-warn/12 text-warn border-warn/25",
    critical: "bg-critical/12 text-critical border-critical/25",
  }[tone];
  return <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-medium border ${t}`}>{children}</span>;
}

export function Segmented<T extends string>({ value, options, onChange }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="flex items-center gap-0.5 panel p-1 w-fit">
      {options.map((o) => (
        <button key={o.value} onClick={() => onChange(o.value)} className={`relative px-3 py-1 rounded-lg text-xs font-medium transition-colors ${value === o.value ? "text-ink" : "text-ink-2 hover:text-ink"}`}>
          {value === o.value && <motion.span layoutId="seg" className="absolute inset-0 rounded-lg" style={{ background: "color-mix(in oklab, var(--accent) 14%, transparent)" }} transition={{ type: "spring", stiffness: 500, damping: 40 }} />}
          <span className="relative">{o.label}</span>
        </button>
      ))}
    </div>
  );
}
