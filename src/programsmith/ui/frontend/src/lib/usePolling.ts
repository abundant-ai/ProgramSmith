import { useCallback, useEffect, useRef, useState } from "react";

interface PollingState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  /** True only on the very first fetch (so polls don't flash skeletons). */
  initialLoading: boolean;
  refresh: () => Promise<void>;
}

/**
 * Poll an async fetcher on an interval, keeping the last good value during
 * background refreshes. Pausing (intervalMs <= 0) stops the timer.
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[] = [],
): PollingState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(async () => {
    // setInterval does not wait for the previous promise. Without this guard a slow fleet read
    // starts another request every tick, then another, creating the very backlog that keeps it
    // slow. Manual refreshes share the same guard.
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
    try {
      const result = await fetcherRef.current();
      if (!mounted.current) return;
      setData(result);
      setError(null);
    } catch (e) {
      if (!mounted.current) return;
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      inFlight.current = false;
      if (mounted.current) {
        setLoading(false);
        setInitialLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const pageIsActive = () =>
      document.visibilityState === "visible" && document.hasFocus();
    const poll = () => {
      // Some embedded browsers report every open tab as `visible`, so visibility alone does not
      // prevent seven background ProgramSmith tabs from polling together. Only the focused tab
      // polls; a tab refreshes immediately when the user returns to it.
      if (pageIsActive()) void refresh();
    };
    poll();
    if (intervalMs > 0) {
      const id = window.setInterval(poll, intervalMs);
      const onVisibility = () => {
        if (pageIsActive()) void refresh();
      };
      const onFocus = () => void refresh();
      document.addEventListener("visibilitychange", onVisibility);
      window.addEventListener("focus", onFocus);
      return () => {
        mounted.current = false;
        window.clearInterval(id);
        document.removeEventListener("visibilitychange", onVisibility);
        window.removeEventListener("focus", onFocus);
      };
    }
    return () => {
      mounted.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, refresh, ...deps]);

  return { data, error, loading, initialLoading, refresh };
}
