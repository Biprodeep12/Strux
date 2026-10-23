import { AnimatePresence, motion } from "framer-motion";

export type Toast = { id: number; title: string; body?: string; kind?: "accent" | "good" | "warn" | "critical"; ttl?: number };

const COLOR: Record<NonNullable<Toast["kind"]>, string> = {
  accent: "var(--accent)", good: "var(--good)", warn: "var(--warn)", critical: "var(--critical)",
};

export default function Toasts({ items }: { items: Toast[] }) {
  return (
    <div className="fixed bottom-5 right-5 z-50 space-y-2 w-[340px] pointer-events-none">
      <AnimatePresence initial={false}>
        {items.map((t) => (
          <motion.div key={t.id} layout initial={{ opacity: 0, y: 12, scale: 0.97 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: 0.97 }} transition={{ type: "spring", stiffness: 420, damping: 32 }}
            className="panel px-4 py-3 pointer-events-auto flex gap-3" style={{ boxShadow: "var(--shadow-lg)" }}>
            <span className="w-1 rounded-full shrink-0" style={{ background: COLOR[t.kind ?? "accent"] }} />
            <div className="min-w-0">
              <div className="text-sm font-medium">{t.title}</div>
              {t.body && <div className="text-xs text-ink-2 mt-0.5 leading-relaxed">{t.body}</div>}
            </div>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
