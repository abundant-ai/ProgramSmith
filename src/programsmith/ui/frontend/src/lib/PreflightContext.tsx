import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api, type Preflight } from "../api";

interface PreflightCtx {
  preflight: Preflight | null;
  loading: boolean;
  refresh: () => Promise<void>;
}

const Ctx = createContext<PreflightCtx>({
  preflight: null,
  loading: true,
  refresh: async () => {},
});

export function PreflightProvider({ children }: { children: ReactNode }) {
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const p = await api.preflight();
      setPreflight(p);
    } catch {
      // Treat an unreachable backend as "not ready" with no checks rather than crashing.
      setPreflight({ ready: false, checks: [] });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <Ctx.Provider value={{ preflight, loading, refresh }}>
      {children}
    </Ctx.Provider>
  );
}

export function usePreflight() {
  return useContext(Ctx);
}
