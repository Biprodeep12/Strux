export function fmtDur(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function fmtTimeS(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

export function ago(ts: number): string {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

export function appLabel(app: string): string {
  const map: Record<string, string> = {
    "code.exe": "VS Code", "brave.exe": "Brave", "chrome.exe": "Chrome", "msedge.exe": "Edge", "windowsterminal.exe": "Terminal",
    "explorer.exe": "Explorer", "slack.exe": "Slack", "discord.exe": "Discord", "notion.exe": "Notion", "spotify.exe": "Spotify",
    "figma.exe": "Figma", "postman.exe": "Postman", "powershell.exe": "PowerShell", "cmd.exe": "CMD", "teams.exe": "Teams",
    "ms-teams.exe": "Teams", "whatsapp.exe": "WhatsApp", "telegram.exe": "Telegram", "obsidian.exe": "Obsidian",
    "excel.exe": "Excel", "winword.exe": "Word", "powerpnt.exe": "PowerPoint", "outlook.exe": "Outlook", "olk.exe": "Outlook",
    "firefox.exe": "Firefox", "notepad.exe": "Notepad", "acrobat.exe": "Acrobat", "acrord32.exe": "Acrobat", "claude.exe": "Claude",
  };
  if (!app) return "—";
  return map[app.toLowerCase()] ?? app.replace(/\.exe$/i, "").replace(/^\w/, (c) => c.toUpperCase());
}

const SERIES = ["var(--color-s1)", "var(--color-s2)", "var(--color-s3)", "var(--color-s4)", "var(--color-s5)", "var(--color-s6)", "var(--color-s7)", "var(--color-s8)"];

/** Stable colour per app for the session: assigned by first appearance, never cycled past 8 (Other = grey). */
const assigned = new Map<string, string>();
export function appColor(app: string): string {
  if (!assigned.has(app)) {
    const idx = assigned.size;
    assigned.set(app, idx < 7 ? SERIES[idx] : SERIES[7]);
  }
  return assigned.get(app)!;
}

const ACTION_LABELS: Record<string, string> = {
  explain_error: "Explain", recap: "Recap", focus: "Start focus", break: "Take break", discuss: "Ask",
  add_todo: "Add TODO", complete_todo: "Mark done", plan: "Plan day", prep_event: "Prep me", digest: "Wrap up",
};

/** Accept-button label for a suggestion's action ("" = plain check). */
export function actionLabel(context: string | Record<string, any> | null | undefined): string {
  try {
    const c = typeof context === "string" ? JSON.parse(context || "{}") : context ?? {};
    return ACTION_LABELS[c?.action?.type] ?? "";
  } catch { return ""; }
}

/** "qwen2.5:7b-instruct-q4_K_M" -> "Qwen2.5 7B", "qwen2.5vl:3b" -> "Qwen2.5-VL 3B", "llama3.1:8b" -> "Llama3.1 8B". */
export function modelLabel(model: string | null | undefined): string {
  if (!model) return "no model";
  if (model.includes(" + ")) return model.split(" + ").map(modelLabel).join(" + ");   // image read by vision, answered by chat model
  const [rawName, tag = ""] = model.split("/").pop()!.split(":");
  let name = rawName.replace(/[-_]?(vl|vision)$/i, (m) => "-" + m.replace(/[-_]/, "").toUpperCase());
  name = name.charAt(0).toUpperCase() + name.slice(1);
  const size = tag.match(/(\d+(?:\.\d+)?)\s*b\b/i) ?? tag.match(/(\d+(?:\.\d+)?)b/i);
  return size ? `${name} ${size[1]}B` : name;
}
