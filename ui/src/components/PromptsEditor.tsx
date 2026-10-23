import { useEffect, useState } from "react";
import { ArrowCounterClockwise, CaretDown, FloppyDisk, TextAlignLeft } from "@phosphor-icons/react";
import { useLiveCtx, useToast } from "../App";
import ModelTag from "./ModelTag";
import { api, type PromptDef } from "../lib/api";
import { Badge, SectionTitle } from "./Bits";

/** Settings → AI prompts: every prompt Strux sends to a model, editable, with reset to default. */
export default function PromptsEditor() {
  const [list, setList] = useState<PromptDef[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const { status } = useLiveCtx();
  useEffect(() => { api.prompts().then((r) => setList(r.prompts)).catch(() => setList([])); }, []);
  return (
    <section className="panel p-5 space-y-3" data-testid="prompts-editor">
      <SectionTitle icon={<TextAlignLeft className="size-3.5" weight="duotone" />}>AI prompts</SectionTitle>
      <p className="text-xs text-ink-3 -mt-1">What Strux tells the model for each feature. Edits apply to the next call; “Reset” goes back to the built-in text. Keep the {"{placeholders}"} listed under each.</p>
      {list == null ? <div className="skeleton h-24" /> : (
        <div className="divide-y divide-line">
          {list.map((p) => <PromptRow key={p.key} p={p} open={open === p.key} onToggle={() => setOpen(open === p.key ? null : p.key)}
            onSaved={(l) => setList(l)} model={p.key === "vision" ? status?.llm?.vision_model : status?.llm?.model} />)}
        </div>
      )}
    </section>
  );
}

function PromptRow({ p, open, onToggle, onSaved, model }: { p: PromptDef; open: boolean; onToggle: () => void; onSaved: (l: PromptDef[]) => void; model?: string }) {
  const toast = useToast();
  const [text, setText] = useState(p.custom ? p.value : p.default);
  const [busy, setBusy] = useState(false);
  useEffect(() => setText(p.custom ? p.value : p.default), [p]);
  const current = p.custom ? p.value : p.default;
  const dirty = text !== current;
  const save = async (value: string | null) => {
    setBusy(true);
    try {
      const r = await api.savePrompts({ [p.key]: value === null || value.trim() === p.default.trim() ? null : value });
      onSaved(r.prompts);
      toast({ title: value === null ? `“${p.title}” reset` : `“${p.title}” saved`, body: "Used from the next model call.", kind: "good" });
    } catch (e: any) { toast({ title: "Save failed", body: String(e), kind: "critical" }); }
    finally { setBusy(false); }
  };
  return (
    <div className="py-2" data-testid={`prompt-${p.key}`}>
      <button className="w-full flex items-center gap-3 text-left py-1" onClick={onToggle}>
        <span className="flex-1 min-w-0">
          <span className="flex items-center gap-2 text-sm font-medium">{p.title}{p.custom && <Badge tone="accent">custom</Badge>}<ModelTag model={model} className="ml-1 font-normal" /></span>
          <span className="block text-[11px] text-ink-3 truncate">{p.used_for}</span>
        </span>
        <CaretDown className={`size-4 text-ink-3 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          <textarea data-testid={`prompt-text-${p.key}`} className="input font-mono text-[12px] leading-relaxed min-h-48" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-ink-3 mr-auto">Placeholders: <span className="font-mono">{p.placeholders}</span></span>
            {p.custom && <button className="btn" disabled={busy} onClick={() => save(null)}><ArrowCounterClockwise className="size-4" />Reset to default</button>}
            {dirty && <button className="btn" disabled={busy} onClick={() => setText(current)}>Discard</button>}
            <button data-testid={`prompt-save-${p.key}`} className="btn btn-primary" disabled={busy || !dirty} onClick={() => save(text)}><FloppyDisk className="size-4" weight="duotone" />Save</button>
          </div>
        </div>
      )}
    </div>
  );
}
