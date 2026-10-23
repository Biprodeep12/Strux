export type Status = {
  month: string; queue: number; dropped: number; events: number; frames: number; suggestions: number;
  summaries: number; frames_by_tier: Record<string, number>; db_files: string[]; paused: boolean;
  brain_ticks: number; brain_skip: string | null; llm_error: string | null; url: string | null; uptime: number;
  llm?: { model: string; vision_model?: string; base_url: string; last_latency: number };
  gpu?: { util: number; free_mb: number; total_mb: number } | null;
  sensors?: Record<string, boolean>;
  screenshot?: { captured: number; deduped: number; blocked: number; ocr_done?: number; ocr_skipped?: number };
  processing?: string;
  overlay_open?: boolean;
  top_processes?: { name: string; pid: number; cpu: number; mem_mb: number }[];
  hot_processes?: string[];
  claude?: { id: string; project: string; status: string; last: string; waiting: boolean; updated: number; prompts: number; tools: number; last_tool: string }[];
  wake?: WakeStatus;
};
export type WakeStatus = { enabled: boolean; state: string; heard: number; woken: number };
export type ReadAloud = "off" | "voice" | "always";
export type VoiceSettings = { read_aloud: ReadAloud; wake: WakeStatus; stt_enabled: boolean };

export type ScreenLook = { thumb?: string | null; app?: string | null; title?: string | null };
export type OllamaStart = { ok: boolean; started?: boolean; restarted?: boolean; seconds?: number; note?: string; error?: string };
export type ChatReply = { ok: boolean; text?: string; error?: string; ollama_down?: boolean; latency?: number; changes?: Change[]; model?: string; id?: number | null; screen?: ScreenLook | null };
export type ChatSummary = { conv: string; started: number; updated: number; n: number; title: string };
export type StoredMessage = { id: number; conv: string; ts: number; role: "user" | "assistant"; content: string; rating: number;
  meta: { model?: string; latency?: number; changes?: Change[]; attachments?: string[]; screen?: ScreenLook | null } };
export type KnowledgeNote = { id: number; source: "claude" | "chat" | "screen"; ts: number; project: string; title: string; body: string; rating: number; extra: any };
export type KnowledgeStats = { claude: number; chat: number; screen: number; projects: string[]; liked: number; disliked: number; conversations: number;
  learner: { status?: string; last_run?: number; last_try?: number; facts?: number; took?: number };
  cfg: { enabled?: boolean; claude?: boolean; screens?: boolean; recall?: boolean; profile?: boolean; every_hours?: number };
  block: { id: number; enabled: boolean; lines: number } | null };

export type Session = { start: number; end: number; app: string; title: string; seconds: number };
export type Marker = { ts: number; kind: string; app: string; text: string };
export type Frame = { id: number; ts: number; app: string; title: string; score: number; tier: string; image_path: string | null; pinned: number; has_text: number };
export type Suggestion = { id: number; ts: number; source: string; text: string; context: string; feedback: string | null };
export type Hit = { kind: string; ref_id: number; ts: number; app: string; title: string; snip: string; rank: number };
export type Summary = { id: number; period: string; start_ts: number; end_ts: number; text: string };
export type AppTotal = { app: string; seconds: number };
export type Todo = { id: number; title: string; notes: string; due_ts: number | null; priority: 0 | 1 | 2; project: string; source: string; created_ts: number; updated_ts: number; done_ts: number | null; files?: number };
export type TodoFile = { id: number; todo_id: number; name: string; mime: string; size: number; created_ts: number };
export type CalEvent = { id: number; title: string; start_ts: number; end_ts: number; all_day: number; location: string; notes: string; source: string };
export type PromptDef = { key: string; title: string; used_for: string; placeholders: string; default: string; value: string; custom: boolean };
export type MemoryBlock = { id: number; title: string; content: string; enabled: boolean; position: number; source: string; updated_ts: number; hint: string };
export type Change = { kind: string; todo?: Todo; event?: CalEvent; id?: number };

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { headers: { "Content-Type": "application/json" }, ...init });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export const api = {
  status: () => j<Status>("/api/status"),
  timeline: (minutes: number) => j<{ sessions: Session[]; markers: Marker[]; now: number }>(`/api/timeline?minutes=${minutes}`),
  apps: (hours: number) => j<{ apps: AppTotal[] }>(`/api/apps?hours=${hours}`),
  frames: (minutes: number, limit = 200) => j<{ frames: Frame[] }>(`/api/frames?minutes=${minutes}&limit=${limit}`),
  explain: (id: number, question?: string) =>
    j<{ ok: boolean; text?: string; error?: string; model?: string; latency?: number }>(`/api/frames/${id}/explain`, {
      method: "POST", body: JSON.stringify(question ? { question } : {}),
    }),
  suggestions: (limit = 50) => j<{ suggestions: Suggestion[] }>(`/api/suggestions?limit=${limit}`),
  feedback: (id: number, value: "accept" | "dismiss" | "snooze") =>
    j<{ ok: boolean }>(`/api/suggestions/${id}/feedback`, { method: "POST", body: JSON.stringify({ value }) }),
  search: (q: string) => j<{ hits: Hit[] }>(`/api/search?q=${encodeURIComponent(q)}`),
  summaries: (period: "day" | "hour") => j<{ summaries: Summary[] }>(`/api/summaries?period=${period}`),
  models: () => j<{ reachable: boolean; models: string[] }>("/api/models"),
  startOllama: () => j<OllamaStart>("/api/ollama/start", { method: "POST", body: "{}" }),
  settings: () => j<any>("/api/settings"),
  saveSettings: (patch: any) => j<{ ok: boolean }>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),
  pause: (paused: boolean) => j<{ paused: boolean }>("/api/control/pause", { method: "POST", body: JSON.stringify({ paused }) }),
  forget: (minutes: number) => j<{ events: number; frames: number; files: number }>("/api/control/forget", { method: "POST", body: JSON.stringify({ minutes }) }),
  think: () => j<{ ok: boolean }>("/api/control/think", { method: "POST", body: "{}" }),
  chat: (messages: { role: string; content: string; attachments?: { name: string; type: string; data: string }[] }[], conv?: string, look?: boolean) =>
    j<ChatReply>("/api/chat", { method: "POST", body: JSON.stringify({ messages, conv, look }) }),
  chats: (limit = 15) => j<{ chats: ChatSummary[] }>(`/api/chats?limit=${limit}`),
  conversation: (conv: string) => j<{ conv: string; messages: StoredMessage[] }>(`/api/chats/${encodeURIComponent(conv)}`),
  deleteConversation: (conv: string) => j<{ ok: boolean }>(`/api/chats/${encodeURIComponent(conv)}`, { method: "DELETE" }),
  rateChat: (id: number, rating: 1 | -1 | 0, why = "") => j<{ ok: boolean }>("/api/chats/rate", { method: "POST", body: JSON.stringify({ id, rating, why }) }),
  knowledge: () => j<KnowledgeStats>("/api/knowledge"),
  learnNow: () => j<{ ok: boolean; status: string; facts?: number }>("/api/knowledge/learn", { method: "POST", body: "{}" }),
  clearKnowledge: (source: "claude" | "chat" | "screen") => j<{ ok: boolean; deleted: number }>("/api/knowledge/clear", { method: "POST", body: JSON.stringify({ source }) }),
  knowledgeNotes: (source?: string, q = "", limit = 30) =>
    j<{ notes: KnowledgeNote[] }>(`/api/knowledge/notes?limit=${limit}${source ? `&source=${source}` : ""}${q ? `&q=${encodeURIComponent(q)}` : ""}`),
  todos: (done = false) => j<{ todos: Todo[] }>(`/api/todos${done ? "?done=1" : ""}`),
  addTodo: (body: { text?: string; title?: string; due?: string | number | null; priority?: number; project?: string; notes?: string }) =>
    j<{ todo: Todo }>("/api/todos", { method: "POST", body: JSON.stringify(body) }),
  patchTodo: (id: number, body: Partial<{ title: string; due: string | number | null; priority: number; project: string; notes: string; done: boolean }>) =>
    j<{ todo: Todo }>(`/api/todos/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteTodo: (id: number) => j<{ ok: boolean }>(`/api/todos/${id}`, { method: "DELETE" }),
  voice: () => j<VoiceSettings>("/api/voice"),
  setVoice: (body: Partial<{ read_aloud: ReadAloud; wake: boolean }>) => j<VoiceSettings>("/api/voice", { method: "PUT", body: JSON.stringify(body) }),
  todoFiles: (id: number) => j<{ files: TodoFile[] }>(`/api/todos/${id}/files`),
  /** Raw bytes as the body: no base64, files up to 25 MB. */
  uploadTodoFile: (id: number, f: File) =>
    j<{ file: TodoFile }>(`/api/todos/${id}/files?name=${encodeURIComponent(f.name || "pasted image.png")}`,
      { method: "POST", body: f, headers: { "Content-Type": f.type || "application/octet-stream" } }),
  todoFileUrl: (fid: number, download = false) => `/api/todo-files/${fid}${download ? "?download=1" : ""}`,
  openTodoFile: (fid: number) => j<{ ok: boolean; shown_in_folder?: boolean }>(`/api/todo-files/${fid}/open`, { method: "POST" }),
  deleteTodoFile: (fid: number) => j<{ ok: boolean }>(`/api/todo-files/${fid}`, { method: "DELETE" }),
  events: (start: number, end: number) => j<{ events: CalEvent[] }>(`/api/calendar?start=${start}&end=${end}`),
  addEvent: (body: { title: string; start: number | string; end?: number | string | null; all_day?: boolean; location?: string; notes?: string }) =>
    j<{ event: CalEvent }>("/api/calendar", { method: "POST", body: JSON.stringify(body) }),
  patchEvent: (id: number, body: Partial<{ title: string; start: number | string; end: number | string; all_day: boolean; location: string; notes: string }>) =>
    j<{ event: CalEvent }>(`/api/calendar/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteEvent: (id: number) => j<{ ok: boolean }>(`/api/calendar/${id}`, { method: "DELETE" }),
  memory: () => j<{ blocks: MemoryBlock[]; context: string }>("/api/memory"),
  addMemory: (title: string, content = "") => j<{ block: MemoryBlock }>("/api/memory", { method: "POST", body: JSON.stringify({ title, content }) }),
  patchMemory: (id: number, body: Partial<{ title: string; content: string; enabled: boolean }>) =>
    j<{ block: MemoryBlock }>(`/api/memory/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteMemory: (id: number) => j<{ ok: boolean }>(`/api/memory/${id}`, { method: "DELETE" }),
  remember: (fact: string) => j<{ block: MemoryBlock; added: boolean }>("/api/memory/remember", { method: "POST", body: JSON.stringify({ fact }) }),
  memoryPreview: (q: string) => j<{ question: string; system: string; context: string; chars: number }>(`/api/memory/preview?q=${encodeURIComponent(q)}`),
  clearTimeline: () => j<{ ok: boolean; events: number; frames: number; files: number; summaries: number }>("/api/control/clear-timeline", { method: "POST", body: JSON.stringify({ confirm: "DELETE" }) }),
  plan: () => j<{ ok: boolean; text?: string; error?: string; model?: string; latency?: number }>("/api/plan", { method: "POST", body: "{}" }),
  prompts: () => j<{ prompts: PromptDef[] }>("/api/prompts"),
  savePrompts: (patch: Record<string, string | null>) => j<{ ok: boolean; prompts: PromptDef[] }>("/api/prompts", { method: "PUT", body: JSON.stringify(patch) }),
};
