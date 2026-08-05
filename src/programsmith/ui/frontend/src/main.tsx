import { StrictMode, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { CssBaseline, ThemeProvider } from "@mui/material";
import "./index.css";
import { App } from "./App";
import { PreflightProvider } from "./lib/PreflightContext";
import { ErrorBoundary } from "./components/ErrorBoundary";
import {
  ColorModeContext,
  createAppTheme,
  type ColorMode,
} from "./theme";

function ThemedApp() {
  const [mode, setMode] = useState<ColorMode>(() => {
    const stored = window.localStorage.getItem("programsmith-color-mode");
    const initial = stored === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = initial;
    return initial;
  });

  useEffect(() => {
    document.documentElement.dataset.theme = mode;
    window.localStorage.setItem("programsmith-color-mode", mode);
  }, [mode]);

  const theme = useMemo(() => createAppTheme(mode), [mode]);
  const colorMode = useMemo(
    () => ({ mode, toggleMode: () => setMode((current) => current === "dark" ? "light" : "dark") }),
    [mode],
  );

  return (
    <ColorModeContext.Provider value={colorMode}>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <ErrorBoundary>
          <BrowserRouter>
            <PreflightProvider>
              <App />
            </PreflightProvider>
          </BrowserRouter>
        </ErrorBoundary>
      </ThemeProvider>
    </ColorModeContext.Provider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemedApp />
  </StrictMode>,
);
