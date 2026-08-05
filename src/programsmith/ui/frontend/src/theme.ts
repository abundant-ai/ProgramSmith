import { createContext, useContext } from "react";
import { createTheme } from "@mui/material/styles";

export type ColorMode = "dark" | "light";

export const ColorModeContext = createContext<{
  mode: ColorMode;
  toggleMode: () => void;
}>({ mode: "dark", toggleMode: () => undefined });

export function useColorMode() {
  return useContext(ColorModeContext);
}

export function createAppTheme(mode: ColorMode) {
  const dark = mode === "dark";
  const background = dark
    ? { default: "#111111", paper: "#181818" }
    : { default: "#f7f7f5", paper: "#ffffff" };
  const text = dark
    ? { primary: "#f2f2f2", secondary: "#a6a6a6" }
    : { primary: "#171717", secondary: "#666666" };
  const divider = dark ? "#343434" : "#dededb";

  return createTheme({
    palette: {
      mode,
      primary: {
        main: dark ? "#f2f2f2" : "#171717",
        contrastText: dark ? "#111111" : "#ffffff",
      },
      background,
      text,
      divider,
      success: { main: dark ? "#58c882" : "#197a45" },
      warning: { main: dark ? "#f0b84d" : "#8a5b00" },
      error: { main: dark ? "#ff7b72" : "#b42318" },
      info: { main: dark ? "#f2f2f2" : "#171717" },
    },
    shape: { borderRadius: 0 },
    typography: {
      fontFamily:
        'Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif',
      h1: { fontSize: 24, lineHeight: 1.2, fontWeight: 600 },
      h2: { fontSize: 18, lineHeight: 1.3, fontWeight: 600 },
      h3: { fontSize: 15, lineHeight: 1.35, fontWeight: 600 },
      button: { fontSize: 13, fontWeight: 600, textTransform: "none" },
      body1: { fontSize: 14 },
      body2: { fontSize: 13 },
    },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          body: { backgroundColor: background.default, color: text.primary },
          "::selection": { backgroundColor: dark ? "#3a3a3a" : "#dededb" },
        },
      },
      MuiPaper: {
        defaultProps: { elevation: 0, square: true },
        styleOverrides: { root: { backgroundImage: "none" } },
      },
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: {
          root: { minHeight: 36, borderRadius: 0, boxShadow: "none", paddingInline: 14, gap: 8 },
        },
      },
      MuiOutlinedInput: {
        styleOverrides: { root: { borderRadius: 0, backgroundColor: background.paper } },
      },
      MuiChip: {
        styleOverrides: { root: { borderRadius: 0, height: 26, fontSize: 12 } },
      },
      MuiDialog: {
        styleOverrides: { paper: { borderRadius: 0, border: `1px solid ${divider}` } },
      },
      MuiTooltip: {
        styleOverrides: {
          tooltip: {
            borderRadius: 0,
            backgroundColor: dark ? "#f2f2f2" : "#171717",
            color: dark ? "#171717" : "#ffffff",
            fontSize: 12,
          },
          arrow: { color: dark ? "#f2f2f2" : "#171717" },
        },
      },
      MuiTab: {
        styleOverrides: { root: { minHeight: 48, textTransform: "none", fontSize: 13 } },
      },
    },
  });
}
