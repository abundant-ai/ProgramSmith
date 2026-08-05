import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Layout } from "./components/Layout";
import { usePreflight } from "./lib/PreflightContext";
import { FleetPage } from "./pages/FleetPage";
import { RunPage } from "./pages/RunPage";
import { DocsPage } from "./pages/DocsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { CostsPage } from "./pages/CostsPage";

export function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const { preflight, loading } = usePreflight();

  useEffect(() => {
    if (loading || !preflight) return;
    if (!preflight.ready && location.pathname === "/") {
      navigate("/settings", { replace: true, state: { firstRun: true } });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, preflight?.ready]);

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<FleetPage />} />
        <Route path="/run/:key" element={<RunPage />} />
        <Route path="/costs" element={<CostsPage />} />
        <Route path="/docs" element={<DocsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}
