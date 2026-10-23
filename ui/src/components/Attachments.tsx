import { useEffect, useRef, useState, type ReactNode } from "react";
import { FileText, ImageSquare, Paperclip, X } from "@phosphor-icons/react";
import { ACCEPT, filesFrom, fmtSize, readAttachment, type Attachment } from "../lib/attach";

/**
 * State for a chat composer: attach button, plus paste and drag & drop *anywhere on the page*
 * (no need to focus the input first). `onActivate` runs when something gets attached (the pane
 * switches to its Chat tab). Pasting files copied in Explorer goes through the backend, because a
 * web page only receives clipboard images, not copied files.
 */
export function useAttachments(onError: (msg: string) => void, onActivate?: () => void) {
  const [items, setItems] = useState<Attachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const cb = useRef({ onError, onActivate });
  cb.current = { onError, onActivate };
  const push = (list: Attachment[]) => { if (list.length) { setItems((l) => [...l, ...list].slice(0, 6)); cb.current.onActivate?.(); } };
  const add = async (files: File[]) => {
    const out: Attachment[] = [];
    for (const f of files.slice(0, 6)) {
      try { out.push(await readAttachment(f)); } catch (e: any) { cb.current.onError(String(e?.message ?? e)); }
    }
    push(out);
  };
  const addRef = useRef(add);
  addRef.current = add;
  const pushRef = useRef(push);
  pushRef.current = push;

  useEffect(() => {
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
    let depth = 0;
    // a file dropped anywhere else would make WebView2 navigate to it: always take the drop
    const over = (e: DragEvent) => { e.preventDefault(); if (hasFiles(e) && e.dataTransfer) e.dataTransfer.dropEffect = "copy"; };
    const enter = (e: DragEvent) => { if (hasFiles(e)) { depth++; setDragging(true); } };
    const leave = (e: DragEvent) => { if (hasFiles(e) && --depth <= 0) { depth = 0; setDragging(false); } };
    const drop = (e: DragEvent) => { e.preventDefault(); depth = 0; setDragging(false); const f = filesFrom(e.dataTransfer); if (f.length) addRef.current(f); };
    const paste = async (e: ClipboardEvent) => {
      const files = filesFrom(e.clipboardData);
      if (files.length) { e.preventDefault(); addRef.current(files); return; }
      const types = Array.from(e.clipboardData?.types ?? []);
      if (types.includes("text/plain") || types.includes("text/html")) return;        // ordinary text paste
      e.preventDefault();                                                              // e.g. files copied in Explorer
      try {
        const r = await fetch("/api/attach/clipboard", { method: "POST" }).then((x) => x.json());
        if (r.attachments?.length) pushRef.current(r.attachments);
        else if (r.problems?.length) cb.current.onError(r.problems.join("; "));
      } catch (err: any) { cb.current.onError(String(err)); }
    };
    document.addEventListener("dragover", over);
    document.addEventListener("dragenter", enter);
    document.addEventListener("dragleave", leave);
    document.addEventListener("drop", drop);
    document.addEventListener("paste", paste);
    return () => {
      document.removeEventListener("dragover", over); document.removeEventListener("dragenter", enter);
      document.removeEventListener("dragleave", leave); document.removeEventListener("drop", drop); document.removeEventListener("paste", paste);
    };
  }, []);

  return {
    items, clear: () => setItems([]), remove: (i: number) => setItems((l) => l.filter((_, j) => j !== i)), dragging,
    addReady: (list: Attachment[]) => push(list),
    open: () => input.current?.click(),
    inputEl: <input ref={input} type="file" multiple accept={ACCEPT} className="hidden" data-testid="attach-input"
      onChange={(e) => { add(Array.from(e.target.files ?? [])); e.target.value = ""; }} />,
  };
}

export function AttachButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <button type="button" className="btn px-2.5 shrink-0" onClick={onClick} disabled={disabled} title="Attach files or images (or paste / drop them)" aria-label="Attach">
      <Paperclip className="size-4" />
    </button>
  );
}

/** Chips above the input; also used read-only inside sent messages. */
export function AttachmentChips({ items, onRemove, small }: { items: Attachment[]; onRemove?: (i: number) => void; small?: boolean }) {
  if (!items.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5" data-testid="attach-chips">
      {items.map((a, i) => (
        <span key={i} className={`relative inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel ${a.preview ? "p-0.5" : "px-2 py-1"} max-w-[220px]`} title={`${a.name} · ${fmtSize(a.size)}`}>
          {a.preview ? <img src={a.preview} alt={a.name} className={`${small ? "h-14" : "h-12"} max-w-[160px] rounded-md object-cover`} />
            : <><FileText className="size-4 text-accent shrink-0" weight="duotone" /><span className="text-[11px] truncate text-ink">{a.name}</span>
              <span className="text-[10px] text-ink-3 shrink-0">{fmtSize(a.size)}</span></>}
          {onRemove && (
            <button type="button" aria-label={`Remove ${a.name}`} onClick={() => onRemove(i)}
              className={`${a.preview ? "absolute -top-1.5 -right-1.5 bg-ink text-white rounded-full size-4 grid place-items-center" : "text-ink-3 hover:text-ink"}`}>
              <X className="size-2.5" weight="bold" />
            </button>
          )}
        </span>
      ))}
    </div>
  );
}

export function DropHint({ show, children }: { show: boolean; children?: ReactNode }) {
  if (!show) return null;
  return (
    <div className="absolute inset-0 z-30 grid place-items-center rounded-2xl border-2 border-dashed border-accent bg-accent/8 pointer-events-none">
      <div className="flex items-center gap-2 text-sm font-medium text-accent"><ImageSquare className="size-5" weight="duotone" />{children ?? "Drop files or images to attach"}</div>
    </div>
  );
}
