import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { Brain, Camera, FloppyDisk, Gauge, Microphone, ShieldCheck, SlidersHorizontal, SpeakerHigh, SpeakerSlash } from "@phosphor-icons/react";
import { useToast } from "../App";
import { api, type ReadAloud, type VoiceSettings } from "../lib/api";
import { PageHeader, SectionTitle, Segmented } from "../components/Bits";
import { setTtsPrefs, speak, stopSpeaking, ttsAvailable, useLocalVoices } from "../components/Voice";
import PromptsEditor from "../components/PromptsEditor";
import DangerZone from "../components/DangerZone";
import OllamaButton from "../components/OllamaButton";

type Cfg = any;

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-xs text-ink-2 mb-1">{label}</div>
      {children}
      {hint && <div className="text-[11px] text-ink-3 mt-1">{hint}</div>}
    </label>
  );
}

function Toggle({ value, onChange, label, hint }: { value: boolean; onChange: (v: boolean) => void; label: string; hint?: string }) {
  return (
    <button type="button" onClick={() => onChange(!value)} className="flex items-center justify-between w-full py-1.5 text-sm gap-4 text-left">
      <span><span className="text-ink">{label}</span>{hint && <span className="block text-[11px] text-ink-3">{hint}</span>}</span>
      <span className="w-9 h-5 rounded-full relative transition-colors shrink-0" style={{ background: value ? "var(--accent)" : "var(--line-2)" }}>
        <motion.span animate={{ x: value ? 16 : 2 }} transition={{ type: "spring", stiffness: 500, damping: 32 }} className="absolute top-0.5 left-0 size-4 rounded-full bg-white shadow" />
      </span>
    </button>
  );
}

/** Voice: applied right away (no Save needed), shared by the dashboard and the bar chat. */
function OllamaStatus() {
  const [up, setUp] = useState<boolean | null>(null);
  const check = () => api.models().then((m) => setUp(m.reachable)).catch(() => setUp(false));
  useEffect(() => { check(); }, []);
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="flex items-center gap-2">
        <span className={`size-2 rounded-full ${up ? "bg-accent" : up === false ? "bg-critical" : "bg-line"}`} />
        {up == null ? "Checking Ollama…" : up ? "Ollama is running" : "Ollama is not running"}
      </span>
      {up === false && <OllamaButton small onUp={check} />}
    </div>
  );
}

function VoiceSettingsCard() {
  const toast = useToast();
  const [v, setV] = useState<VoiceSettings | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.voice().then(setV).catch(() => {}); }, []);
  const update = async (body: Parameters<typeof api.setVoice>[0]) => {
    setBusy(true);
    try { setV(await api.setVoice(body)); } catch (e: any) { toast({ title: "Couldn't change voice settings", body: String(e), kind: "critical" }); }
    finally { setBusy(false); }
  };
  if (!v) return null;
  const st = v.wake.state;
  return (
    <section className="panel p-5 space-y-3" data-testid="voice-settings">
      <SectionTitle icon={<Microphone className="size-3.5" weight="duotone" />}>Voice</SectionTitle>
      <Toggle label="Listen for “Hey Strux”" value={v.wake.enabled} onChange={(on) => !busy && update({ wake: on })}
        hint="Say “Hey Strux” and then your message; Strux waits until you stop talking and sends it in the bar chat. Keeps the microphone open while on. Everything stays on this PC; no audio is saved." />
      {v.wake.enabled && (
        <div className={`text-[11px] ${st.startsWith("error") ? "text-critical" : "text-ink-3"}`}>
          {st.startsWith("error") ? `Not listening: ${st.slice(7)}` : st === "off" ? "Starting… (the first time downloads a 75 MB model)" :
            `Listening for “Hey Strux” · ${v.wake.woken} wake-up${v.wake.woken === 1 ? "" : "s"} so far. Pauses while Strux is paused or the PC is locked.`}
        </div>
      )}
      <Field label="Read replies aloud" hint="Uses Windows' own voices, on this PC. Every reply also has a Read button.">
        <select data-testid="read-aloud-setting" className="input" value={v.read_aloud} onChange={(e) => update({ read_aloud: e.target.value as ReadAloud })}>
          <option value="off">Off</option>
          <option value="voice">When I speak (mic or “Hey Strux”)</option>
          <option value="always">Every reply</option>
        </select>
      </Field>
      <VoicePicker v={v} update={update} />
    </section>
  );
}

const SPEEDS = [{ v: 0.85, label: "Slow" }, { v: 1.05, label: "Normal" }, { v: 1.2, label: "Fast" }, { v: 1.4, label: "Faster" }];
const SAMPLE = "Hi, I'm Strux. You have two TODOs due today, and your next meeting starts at four.";

/** Which Windows voice reads replies, and how fast. Only voices installed on this PC are listed. */
function VoicePicker({ v, update }: { v: VoiceSettings; update: (b: Parameters<typeof api.setVoice>[0]) => void }) {
  const voices = useLocalVoices();
  const [playing, setPlaying] = useState(false);
  useEffect(() => { setTtsPrefs(v); }, [v]);
  const preview = (p: { tts_voice?: string; tts_rate?: number } = {}) => {
    setTtsPrefs({ tts_voice: v.tts_voice, tts_rate: v.tts_rate, ...p });
    if (speak(SAMPLE, "voice-preview", () => setPlaying(false))) setPlaying(true);
  };
  const choose = (p: { tts_voice?: string; tts_rate?: number }) => { update(p); preview(p); };
  const label = (x: SpeechSynthesisVoice) => `${x.name.replace(/^Microsoft\s+/, "").replace(/\s*-\s*.*$/, "").replace(/\s+Online.*$/, "")} · ${x.lang}`;
  if (!ttsAvailable()) return null;
  return (
    <>
      <Field label="Voice" hint={voices.length ? `${voices.length} voice${voices.length === 1 ? "" : "s"} on this PC. More: Windows Settings → Time & language → Speech → Add voices.` : "Loading the voices on this PC…"}>
        <div className="flex gap-2">
          <select data-testid="tts-voice" className="input" value={v.tts_voice} onChange={(e) => choose({ tts_voice: e.target.value })}>
            <option value="">Automatic (best English voice)</option>
            {v.tts_voice && !voices.some((x) => x.name === v.tts_voice) && <option value={v.tts_voice}>{v.tts_voice} (not installed)</option>}
            {voices.map((x) => <option key={x.voiceURI} value={x.name}>{label(x)}</option>)}
          </select>
          <button type="button" data-testid="tts-preview" className="btn px-3 shrink-0" onClick={() => (playing ? (stopSpeaking(), setPlaying(false)) : preview())}
            title={playing ? "Stop" : "Hear this voice"}>
            {playing ? <SpeakerSlash className="size-4" weight="duotone" /> : <SpeakerHigh className="size-4" weight="duotone" />}{playing ? "Stop" : "Try"}
          </button>
        </div>
      </Field>
      <Field label="Speed">
        <Segmented value={String(SPEEDS.reduce((a, b) => (Math.abs(b.v - v.tts_rate) < Math.abs(a.v - v.tts_rate) ? b : a)).v)}
          onChange={(s) => choose({ tts_rate: Number(s) })} options={SPEEDS.map((s) => ({ value: String(s.v), label: s.label }))} />
      </Field>
    </>
  );
}

const lines = (arr: string[] | undefined) => (arr ?? []).join("\n");
const parse = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);

export default function SettingsPage() {
  const toast = useToast();
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  useEffect(() => { api.settings().then(setCfg); }, []);
  if (!cfg) return <div className="space-y-3"><div className="skeleton h-8 w-40" /><div className="skeleton h-64" /></div>;

  const set = (path: string, v: any) => {
    setDirty(true);
    setCfg((c: Cfg) => {
      const n = structuredClone(c);
      const keys = path.split(".");
      let o = n;
      for (const k of keys.slice(0, -1)) o = o[k];
      o[keys[keys.length - 1]] = v;
      return n;
    });
  };
  const num = (path: string, v: string) => set(path, Number(v));

  const save = async () => {
    setSaving(true);
    try { const { prompts: _p, ...rest } = cfg; void _p; await api.saveSettings(rest); setDirty(false); toast({ title: "Settings saved", body: "Written to config.yaml and applied live.", kind: "good" }); }
    catch (e: any) { toast({ title: "Save failed", body: String(e), kind: "critical" }); }
    finally { setSaving(false); }
  };

  const ss = cfg.sensors.screenshot, r = cfg.retention, b = cfg.brain, l = cfg.brain.llm, ov = cfg.ui.overlay ?? {};

  return (
    <div className="space-y-4">
      <PageHeader title="Settings" subtitle={<>Saved to <span className="font-mono">config.yaml</span>. Most changes apply immediately; sensor on/off needs a restart.</>}
        right={<button className="btn btn-primary" onClick={save} disabled={saving || !dirty}><FloppyDisk className="size-4" weight="duotone" />{saving ? "Saving…" : dirty ? "Save changes" : "Saved"}</button>} />

      <div className="grid lg:grid-cols-2 gap-4 items-start">
        <div className="space-y-4">
        <section className="panel p-5 space-y-4">
          <SectionTitle icon={<ShieldCheck className="size-3.5" weight="duotone" />}>Privacy</SectionTitle>
          <Field label="Never capture these apps (one per line)" hint="No pixels are read while these are in front.">
            <textarea className="input font-mono h-24 text-[13px]" value={lines(ss.blocklist_apps)} onChange={(e) => set("sensors.screenshot.blocklist_apps", parse(e.target.value))} />
          </Field>
          <Field label="Never capture when the title contains" hint="Case-insensitive.">
            <textarea className="input font-mono h-20 text-[13px]" value={lines(ss.blocklist_title_keywords)} onChange={(e) => set("sensors.screenshot.blocklist_title_keywords", parse(e.target.value))} />
          </Field>
          <Field label="Never suggest while in these apps" hint="Games, recordings, presentations…">
            <textarea className="input font-mono h-14 text-[13px]" placeholder="e.g. game.exe" value={lines(b.never_suggest_apps)} onChange={(e) => set("brain.never_suggest_apps", parse(e.target.value))} />
          </Field>
        </section>

        <VoiceSettingsCard />

        <section className="panel p-5 space-y-4">
          <SectionTitle icon={<Brain className="size-3.5" weight="duotone" />}>Model</SectionTitle>
          <Field label="Your name" hint="How Strux greets and addresses you. Empty = the first name of your Windows account.">
            <input className="input" value={cfg.user?.name ?? ""} placeholder="(from Windows)" onChange={(e) => set("user.name", e.target.value)} />
          </Field>
          <OllamaStatus />
          <Toggle label="Use the local model for suggestions" value={l.enabled} onChange={(v) => set("brain.llm.enabled", v)} />
          <div className="grid grid-cols-2 gap-3">
            <Field label="Provider"><select className="input" value={l.provider} onChange={(e) => set("brain.llm.provider", e.target.value)}><option value="ollama">Ollama</option><option value="openai">OpenAI-compatible</option></select></Field>
            <Field label="Base URL"><input className="input font-mono" value={l.base_url} onChange={(e) => set("brain.llm.base_url", e.target.value)} /></Field>
            <Field label="Chat model"><input className="input font-mono" value={l.model} onChange={(e) => set("brain.llm.model", e.target.value)} /></Field>
            <Field label="Vision model"><input className="input font-mono" value={l.vision_model ?? ""} onChange={(e) => set("brain.llm.vision_model", e.target.value)} /></Field>
            <Field label="Ask every (min)"><input type="number" className="input" value={l.interval_minutes} onChange={(e) => num("brain.llm.interval_minutes", e.target.value)} /></Field>
            <Field label="Context (tokens)"><input type="number" className="input" value={l.num_ctx} onChange={(e) => num("brain.llm.num_ctx", e.target.value)} /></Field>
            <Field label="Skip if GPU busier than (%)"><input type="number" className="input" value={l.max_gpu_util_percent} onChange={(e) => num("brain.llm.max_gpu_util_percent", e.target.value)} /></Field>
            <Field label="Need free VRAM (MB)"><input type="number" className="input" value={l.min_free_vram_mb} onChange={(e) => num("brain.llm.min_free_vram_mb", e.target.value)} /></Field>
          </div>
        </section>

        </div>
        <div className="space-y-4">
        <section className="panel p-5 space-y-4">
          <SectionTitle icon={<Camera className="size-3.5" weight="duotone" />}>Screens</SectionTitle>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Interval (s)"><input type="number" className="input" value={ss.interval_seconds} onChange={(e) => num("sensors.screenshot.interval_seconds", e.target.value)} /></Field>
            <Field label="Min gap (s)"><input type="number" className="input" value={ss.min_gap_seconds} onChange={(e) => num("sensors.screenshot.min_gap_seconds", e.target.value)} /></Field>
            <Field label="Dedupe threshold (bits)" hint="Lower = stricter"><input type="number" className="input" value={ss.phash_min_distance} onChange={(e) => num("sensors.screenshot.phash_min_distance", e.target.value)} /></Field>
            <Field label="Video: one frame per (s)"><input type="number" className="input" value={ss.video_interval_seconds ?? 90} onChange={(e) => num("sensors.screenshot.video_interval_seconds", e.target.value)} /></Field>
          </div>
          <Toggle label="Capture on window change" value={ss.on_window_change} onChange={(v) => set("sensors.screenshot.on_window_change", v)} />
          <Toggle label="Read screen text (OCR)" hint="CPU only, idle priority, at most one frame every 20 s" value={ss.ocr} onChange={(v) => set("sensors.screenshot.ocr", v)} />

          <SectionTitle>Retention</SectionTitle>
          <div className="grid grid-cols-3 gap-3">
            <Field label="Hot (hours)"><input type="number" className="input" value={r.hot_hours} onChange={(e) => num("retention.hot_hours", e.target.value)} /></Field>
            <Field label="Warm (days)"><input type="number" className="input" value={r.warm_days} onChange={(e) => num("retention.warm_days", e.target.value)} /></Field>
            <Field label="Cold (days)"><input type="number" className="input" value={r.cold_days} onChange={(e) => num("retention.cold_days", e.target.value)} /></Field>
            <Field label="Warm below score"><input type="number" className="input" value={r.warm_below_score} onChange={(e) => num("retention.warm_below_score", e.target.value)} /></Field>
            <Field label="Drop below score"><input type="number" className="input" value={r.drop_below_score} onChange={(e) => num("retention.drop_below_score", e.target.value)} /></Field>
            <Field label="Disk cap (MB)"><input type="number" className="input" value={r.disk_cap_mb} onChange={(e) => num("retention.disk_cap_mb", e.target.value)} /></Field>
          </div>
        </section>

        <section className="panel p-5 space-y-4">
          <SectionTitle icon={<SlidersHorizontal className="size-3.5" weight="duotone" />}>Rules</SectionTitle>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Global cooldown (min)"><input type="number" className="input" value={b.global_cooldown_minutes} onChange={(e) => num("brain.global_cooldown_minutes", e.target.value)} /></Field>
            <Field label="Skip model if CPU above (%)"><input type="number" className="input" value={b.max_cpu_percent} onChange={(e) => num("brain.max_cpu_percent", e.target.value)} /></Field>
          </div>
          <Toggle label="Stay quiet while idle" value={b.quiet_when_idle} onChange={(v) => set("brain.quiet_when_idle", v)} />
          <div className="divide-y divide-line">
            {Object.entries(b.rules as Record<string, any>).map(([name, rc]) => (
              <div key={name} className="py-1.5 flex items-center gap-3">
                <div className="flex-1"><Toggle label={name.replace(/_/g, " ")} value={rc.enabled} onChange={(v) => set(`brain.rules.${name}.enabled`, v)} /></div>
                <div className="w-24"><input type="number" className="input py-1" title="cooldown minutes" value={rc.cooldown_minutes} onChange={(e) => num(`brain.rules.${name}.cooldown_minutes`, e.target.value)} /></div>
                <span className="text-[11px] text-ink-3 w-16">min cooldown</span>
              </div>
            ))}
          </div>

          <SectionTitle icon={<Gauge className="size-3.5" weight="duotone" />}>Bar & interface</SectionTitle>
          <Toggle label="Strux bar always on screen" hint="Off = the bar only appears when there is something to show" value={ov.always_visible ?? true} onChange={(v) => set("ui.overlay.always_visible", v)} />
          <Field label="Auto-collapse after (s)"><input type="number" className="input" value={ov.timeout_seconds ?? 14} onChange={(e) => num("ui.overlay.timeout_seconds", e.target.value)} /></Field>
          <Toggle label="Windows toast notifications" hint="Only used when the bar is disabled" value={cfg.ui.toasts} onChange={(v) => set("ui.toasts", v)} />
          <Toggle label="Start minimised to tray" value={cfg.ui.start_hidden} onChange={(v) => set("ui.start_hidden", v)} />
          <div className="text-[11px] text-ink-3 pt-1 space-x-2">
            <span className="kbd">Ctrl+Alt+Space</span> chat <span className="kbd">Ctrl+Alt+W</span> dashboard <span className="kbd">Ctrl+Alt+P</span> pause <span className="kbd">Ctrl+Alt+F</span> forget 10 min
          </div>
        </section>
        </div>
      </div>
      <PromptsEditor />
      <DangerZone />
    </div>
  );
}
