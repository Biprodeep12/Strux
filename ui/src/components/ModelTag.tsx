import { Cpu } from "@phosphor-icons/react";
import { modelLabel } from "../lib/format";

/** Which local model produced an answer (full name + "runs on this PC" on hover). */
export default function ModelTag({ model, latency, className = "" }: { model?: string | null; latency?: number | null; className?: string }) {
  if (!model) return null;
  return (
    <span data-testid="model-tag" title={`${model} · runs locally on this PC`}
      className={`inline-flex items-center gap-1 text-[10px] text-ink-3 font-medium whitespace-nowrap ${className}`}>
      <Cpu className="size-3" weight="duotone" />{modelLabel(model)}{latency != null ? <span className="font-mono font-normal">· {latency}s</span> : null}
    </span>
  );
}
