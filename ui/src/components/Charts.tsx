import { memo, useMemo, useState } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { AppTotal, Session } from "../lib/api";
import { appColor, appLabel, fmtDur } from "../lib/format";

/** Horizontal bar list: time per app. Direct labels, hover tooltip, folds >7 into Other. */
export const AppBars = memo(function AppBars({ apps }: { apps: AppTotal[] }) {
  const rows = useMemo(() => {
    const top = apps.slice(0, 7);
    const rest = apps.slice(7).reduce((a, b) => a + b.seconds, 0);
    const list = rest > 0 ? [...top, { app: "other", seconds: rest }] : top;
    const max = Math.max(1, ...list.map((a) => a.seconds));
    return list.map((a) => ({ ...a, pct: (a.seconds / max) * 100 }));
  }, [apps]);
  const total = apps.reduce((a, b) => a + b.seconds, 0);
  if (!rows.length) return <div className="text-sm text-ink-3 py-6 text-center">No activity yet.</div>;
  return (
    <div className="space-y-2.5">
      {rows.map((r) => (
        <div key={r.app} className="group grid grid-cols-[120px_1fr_64px] items-center gap-3 text-sm" title={`${appLabel(r.app)} · ${fmtDur(r.seconds)} · ${Math.round((r.seconds / total) * 100)}%`}>
          <div className="truncate text-ink-2 flex items-center gap-2">
            <span className="size-2 rounded-full shrink-0" style={{ background: r.app === "other" ? "var(--color-s8)" : appColor(r.app) }} />
            <span className="truncate">{r.app === "other" ? "Other" : appLabel(r.app)}</span>
          </div>
          <div className="h-2.5 rounded-full bg-panel-2 overflow-hidden">
            <div className="h-full rounded-full transition-[width] duration-500 group-hover:brightness-125" style={{ width: `${r.pct}%`, background: r.app === "other" ? "var(--color-s8)" : appColor(r.app) }} />
          </div>
          <div className="text-right font-mono text-xs text-ink-2 tabular-nums">{fmtDur(r.seconds)}</div>
        </div>
      ))}
    </div>
  );
});

/** Active minutes per hour over the last 24 h, from sessions. Single series, crosshair tooltip. */
export const HourlyArea = memo(function HourlyArea({ sessions, now }: { sessions: Session[]; now: number }) {
  const data = useMemo(() => {
    const buckets: { h: number; label: string; minutes: number }[] = [];
    const start = Math.floor((now - 24 * 3600) / 3600) * 3600;
    for (let i = 0; i < 25; i++) {
      const t = start + i * 3600;
      buckets.push({ h: t, label: new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit" }), minutes: 0 });
    }
    for (const s of sessions) {
      let a = s.start;
      while (a < s.end) {
        const idx = Math.floor((a - start) / 3600);
        const edge = start + (idx + 1) * 3600;
        const b = Math.min(s.end, edge);
        if (idx >= 0 && idx < buckets.length) buckets[idx].minutes += (b - a) / 60;
        a = b;
      }
    }
    return buckets.map((b) => ({ ...b, minutes: Math.round(b.minutes) }));
  }, [sessions, now]);
  const [hover, setHover] = useState<string | null>(null);
  return (
    <div className="h-44 -mx-2">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }} onMouseLeave={() => setHover(null)}>
          <defs>
            <linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--color-accent)" stopOpacity={0.5} />
              <stop offset="100%" stopColor="var(--color-accent)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="label" tick={{ fill: "var(--color-ink-3)", fontSize: 11 }} axisLine={false} tickLine={false} interval={3} />
          <YAxis tick={{ fill: "var(--color-ink-3)", fontSize: 11 }} axisLine={false} tickLine={false} width={28} domain={[0, 60]} ticks={[0, 30, 60]} />
          <Tooltip
            cursor={{ stroke: "var(--color-line-2)", strokeDasharray: "3 3" }}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null;
              const p = payload[0].payload as { label: string; minutes: number };
              return (
                <div className="panel px-3 py-2 text-xs shadow-lg">
                  <div className="text-ink-3">{p.label}</div>
                  <div className="font-medium">{p.minutes} active min</div>
                </div>
              );
            }}
          />
          <Area type="monotone" dataKey="minutes" stroke="var(--color-accent-2)" strokeWidth={2} fill="url(#g1)" dot={false} activeDot={{ r: 4, fill: "var(--color-accent-2)", stroke: "var(--color-bg)", strokeWidth: 2 }} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
});
