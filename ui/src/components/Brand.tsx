import { motion } from "framer-motion";

/** The Strux mark: a ring with one orbiting dot on a blue tile (same drawing as assets/make_icon.py). */
export function StruxMark({ size = 32, glow = false }: { size?: number; glow?: boolean }) {
  return (
    <motion.div
      className="relative grid place-items-center rounded-[24%] shrink-0"
      style={{ width: size, height: size, background: "linear-gradient(135deg, #58a0ff, #2554d6)" }}
      animate={glow ? { boxShadow: ["0 0 0 0 color-mix(in oklab, var(--accent) 45%, transparent)", "0 0 0 10px color-mix(in oklab, var(--accent) 0%, transparent)"] } : undefined}
      transition={glow ? { duration: 1.6, repeat: 1, ease: "easeOut", delay: 0.4 } : undefined}
    >
      <svg viewBox="0 0 32 32" width={size} height={size} aria-hidden>
        {/* ring of r 8.16, broken around the dot at -50° (gap r 3.7) */}
        <path d="M 23.46 12.69 A 8.16 8.16 0 1 1 17.97 8.08" fill="none" stroke="white" strokeWidth="2.8" strokeLinecap="butt" />
        <circle cx="21.25" cy="9.75" r="2.3" fill="white" />
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
