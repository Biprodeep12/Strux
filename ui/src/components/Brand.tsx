import { motion } from "framer-motion";

/** The Strux mark: a geometric S ending in an orbiting dot, on a blue tile (same drawing as assets/make_icon.py). */
export function StruxMark({ size = 32, glow = false }: { size?: number; glow?: boolean }) {
  return (
    <motion.div
      className="relative grid place-items-center rounded-[24%] shrink-0"
      style={{ width: size, height: size, background: "linear-gradient(135deg, #58a0ff, #2554d6)" }}
      animate={glow ? { boxShadow: ["0 0 0 0 color-mix(in oklab, var(--accent) 45%, transparent)", "0 0 0 10px color-mix(in oklab, var(--accent) 0%, transparent)"] } : undefined}
      transition={glow ? { duration: 1.6, repeat: 1, ease: "easeOut", delay: 0.4 } : undefined}
    >
      <svg viewBox="0 0 32 32" width={size} height={size} aria-hidden>
        {/* a geometric S of two half-round turns (r 3.25); its top stroke ends in the dot (same as watcher/ui/brand.py) */}
        <path d="M 17.1 9.5 H 11.85 A 3.25 3.25 0 0 0 11.85 16 H 18.35 A 3.25 3.25 0 0 1 18.35 22.5 H 9.1" fill="none" stroke="white"
          strokeWidth="2.8" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx="22.1" cy="9.5" r="2.2" fill="white" />
      </svg>
    </motion.div>
  );
}

export function Wordmark({ sub }: { sub?: string }) {
  return (
    <div className="flex items-center gap-3">
      <StruxMark size={36} />
      <div>
        <div className="font-semibold leading-tight tracking-tight">Strux</div>
        {sub && <div className="text-[11px] text-ink-3">{sub}</div>}
      </div>
    </div>
  );
}
