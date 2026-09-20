"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, toApiError, type ApiError } from "@/lib/api";
import { useEvents } from "@/lib/events";

interface State<T> { data: T | null; error: ApiError | null; loading: boolean }

/** GET a resource; refetch when a matching live event arrives (or on an interval). */
export function useApi<T>(path: string | null, opts: { refreshOn?: string[]; intervalMs?: number } = {}) {
  const [state, setState] = useState<State<T>>({ data: null, error: null, loading: path !== null });
  const { subscribe } = useEvents();
  const seq = useRef(0);
  const refreshKey = (opts.refreshOn ?? []).join(",");

  const load = useCallback(async () => {
    if (!path) return;
    const id = ++seq.current;
    try {
      const data = await api<T>(path);
      if (id === seq.current) setState({ data, error: null, loading: false });
    } catch (e) {
      if (id === seq.current) setState((s) => ({ ...s, error: toApiError(e), loading: false }));
    }
  }, [path]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!refreshKey) return;
    const types = refreshKey.split(",");
    let t: ReturnType<typeof setTimeout> | undefined;
    const off = subscribe((ev) => { if (types.includes(ev.type)) { clearTimeout(t); t = setTimeout(() => void load(), 400); } });
    return () => { off(); clearTimeout(t); };
  }, [subscribe, load, refreshKey]);

  useEffect(() => {
    if (!opts.intervalMs) return;
    const i = setInterval(() => void load(), opts.intervalMs);
    return () => clearInterval(i);
  }, [load, opts.intervalMs]);

  const reload = useCallback(() => { setState((s) => ({ ...s, loading: true })); return load(); }, [load]);
  return { ...state, reload };
}
