/** Chat attachments: images (shrunk in the browser to <= 1280 px JPEG) and documents (sent as-is, base64). */
export type Attachment = { name: string; type: string; size: number; data: string; preview?: string };

export const ACCEPT = "image/*,.txt,.md,.json,.csv,.tsv,.log,.yaml,.yml,.toml,.xml,.html,.css,.js,.jsx,.ts,.tsx,.py,.java,.kt,.go,.rs,.c,.h,.cpp,.cs,.rb,.php,.sql,.sh,.ps1,.pdf,.docx";
const MAX_FILE = 15 * 1024 * 1024;

const isImage = (f: File) => f.type.startsWith("image/");

function readAs(f: File, how: "dataURL"): Promise<string> {
  return new Promise((ok, fail) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result));
    r.onerror = () => fail(r.error);
    if (how === "dataURL") r.readAsDataURL(f);
  });
}

async function shrink(dataUrl: string, max = 1280): Promise<string> {
  const img = new Image();
  await new Promise((ok, fail) => { img.onload = ok; img.onerror = fail; img.src = dataUrl; });
  const scale = Math.min(1, max / Math.max(img.width, img.height));
  const c = document.createElement("canvas");
  c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
  const g = c.getContext("2d")!;
  g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);        // transparent PNGs -> white, not black
  g.drawImage(img, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.86);
}

export async function readAttachment(f: File): Promise<Attachment> {
  if (f.size > MAX_FILE) throw new Error(`${f.name} is larger than 15 MB`);
  const url = await readAs(f, "dataURL");
  if (isImage(f)) {
    const small = await shrink(url);
    return { name: f.name || "pasted image.png", type: "image/jpeg", size: f.size, data: small.split(",")[1], preview: small };
  }
  return { name: f.name, type: f.type || "application/octet-stream", size: f.size, data: url.split(",")[1] };
}

export const fmtSize = (n: number) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${Math.round(n / 1024)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);

/** Files from a paste or drop event (images pasted from the clipboard included). */
export function filesFrom(dt: DataTransfer | null): File[] {
  if (!dt) return [];
  const out: File[] = [];
  for (const it of Array.from(dt.items ?? [])) if (it.kind === "file") { const f = it.getAsFile(); if (f) out.push(f); }
  if (!out.length) out.push(...Array.from(dt.files ?? []));
  return out;
}
