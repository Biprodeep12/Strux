import { createContext, lazy, Suspense, useContext, useEffect, useState } from "react";
import { Brain, CalendarBlank, ChatCircleDots, ClockCounterClockwise, Gear, ListChecks, MagnifyingGlass, Sparkle, SquaresFour } from "@phosphor-icons/react";
import { useLive } from "./lib/live";
import { useTheme } from "./lib/theme";
import Sidebar, { type NavItem } from "./components/Sidebar";
// pages are separate chunks: the pane window never downloads/parses the charts and dashboard pages
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Timeline = lazy(() => import("./pages/Timeline"));
const TodosPage = lazy(() => import("./pages/Todos"));
const CalendarPage = lazy(() => import("./pages/Calendar"));
const MemoryPage = lazy(() => import("./pages/Memory"));
const SearchPage = lazy(() => import("./pages/Search"));
const Suggestions = lazy(() => import("./pages/Suggestions"));
const Ask = lazy(() => import("./pages/Ask"));
const SettingsPage = lazy(() => import("./pages/Settings"));
const Pane = lazy(() => import("./pages/Pane"));
import ErrorBoundary from "./components/ErrorBoundary";
import Toasts, { type Toast } from "./components/Toasts";

export const LiveCtx = createContext<ReturnType<typeof useLive>>({ connected: false, status: null, events: [], suggestions: [], overlay: [], planner: 0 });
export const ToastCtx = createContext<(t: Omit<Toast, "id">) => void>(() => {});
export const ThemeCtx = createContext<{ theme: "light" | "dark"; setTheme: (t: "light" | "dark") => void }>({ theme: "light", setTheme: () => {} });
export const useLiveCtx = () => useContext(LiveCtx);
export const useToast = () => useContext(ToastCtx);
export const useThemeCtx = () => useContext(ThemeCtx);

const NAV: NavItem[] = [
  { id: "dashboard", label: "Dashboard", icon: SquaresFour },
  { id: "todos", label: "TODOs", icon: ListChecks },
  { id: "calendar", label: "Calendar", icon: CalendarBlank },
  { id: "memory", label: "Memory", icon: Brain },
  { id: "timeline", label: "Timeline", icon: ClockCounterClockwise },
  { id: "suggestions", label: "Suggestions", icon: Sparkle },
  { id: "search", label: "Search", icon: MagnifyingGlass },
  { id: "ask", label: "Ask", icon: ChatCircleDots },
  { id: "settings", label: "Settings", icon: Gear },
];

function useHashRoute(): [string, (r: string) => void] {
  const read = () => (location.hash.replace(/^#\/?/, "") || "dashboard").split("?")[0];
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const h = () => setRoute(read());
    addEventListener("hashchange", h);
    return () => removeEventListener("hashchange", h);
  }, []);
  return [route, (r) => (location.hash = `/${r}`)];
}

/** Remount the page when the window comes back after a while: fresh data, and nothing left half-animated. */
function useWakeKey(): number {
  const [key, setKey] = useState(0);
  useEffect(() => {
    let hiddenAt = 0;
    const h = () => {
      if (document.hidden) hiddenAt = Date.now();
      else if (hiddenAt && Date.now() - hiddenAt > 60_000) setKey((k) => k + 1);
    };
    document.addEventListener("visibilitychange", h);
    return () => document.removeEventListener("visibilitychange", h);
  }, []);
  return key;
}

export default function App() {
  const isPane = location.hash.startsWith("#/pane");
  const live = useLive(isPane ? { eventKinds: ["claude"] } : undefined);
  const wake = useWakeKey();
  const [theme, setTheme] = useTheme();
  const [route, go] = useHashRoute();
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = (t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setToasts((ts) => [...ts, { ...t, id }]);
    setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), t.ttl ?? 4500);
  };

  useEffect(() => {
    let g = false;
    const h = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.key === "/") { e.preventDefault(); go("search"); return; }
      if (e.key === "g") { g = true; setTimeout(() => (g = false), 800); return; }
      if (g) { const m: Record<string, string> = { d: "dashboard", o: "todos", l: "calendar", m: "memory", t: "timeline", s: "suggestions", a: "ask", c: "settings" }; if (m[e.key]) go(m[e.key]); g = false; }
    };
    addEventListener("keydown", h);
    return () => removeEventListener("keydown", h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const lastSugg = live.suggestions[0]?.id;
  useEffect(() => {
    if (lastSugg != null) push({ title: "New suggestion", body: live.suggestions[0].text, kind: "accent", ttl: 8000 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastSugg]);

  const Page = { dashboard: Dashboard, todos: TodosPage, calendar: CalendarPage, memory: MemoryPage, timeline: Timeline, suggestions: Suggestions, search: SearchPage, ask: Ask, settings: SettingsPage }[route] ?? Dashboard;

  if (route === "pane") {
    return (
      <ThemeCtx.Provider value={{ theme, setTheme }}>
        <LiveCtx.Provider value={live}>
          <ErrorBoundary name="pane"><Suspense fallback={null}><Pane /></Suspense></ErrorBoundary>
        </LiveCtx.Provider>
      </ThemeCtx.Provider>
    );
  }

  return (
    <ThemeCtx.Provider value={{ theme, setTheme }}>
      <LiveCtx.Provider value={live}>
        <ToastCtx.Provider value={push}>
          <div className="h-full flex">
            <Sidebar items={NAV} active={route} onSelect={go} />
            <main className="flex-1 min-w-0 h-full overflow-y-auto">
              {/* CSS-only page fade: no JS animation that can stall at opacity 0 while the window is hidden */}
              <div key={`${route}-${wake}`} className="page-in max-w-[1400px] mx-auto px-6 py-6 lg:px-8">
                <ErrorBoundary name={route}><Suspense fallback={<div className="skeleton h-40" />}><Page /></Suspense></ErrorBoundary>
              </div>
            </main>
            <Toasts items={toasts} />
          </div>
        </ToastCtx.Provider>
      </LiveCtx.Provider>
    </ThemeCtx.Provider>
  );
}
