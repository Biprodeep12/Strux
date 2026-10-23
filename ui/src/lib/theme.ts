import { useEffect, useState } from "react";

export type Theme = "light" | "dark";
const KEY = "watcher.theme";

export function readTheme(): Theme {
  try {
    const t = localStorage.getItem(KEY);
    if (t === "light" || t === "dark") return "light";
  } catch { /* storage may be blocked */ }
  return "light";
}

export function applyTheme(t: Theme) {
  document.documentElement.classList.remove("dark");   // Strux is light only
  try { localStorage.setItem(KEY, t); } catch { /* ignore */ }
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, set] = useState<Theme>(readTheme);
  useEffect(() => { applyTheme(theme); }, [theme]);
  useEffect(() => {
    const onStorage = (e: StorageEvent) => { if (e.key === KEY && (e.newValue === "light" || e.newValue === "dark")) set(e.newValue); };
    addEventListener("storage", onStorage);
    return () => removeEventListener("storage", onStorage);
  }, []);
  return [theme, set];
}
