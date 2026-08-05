import type { ReactNode } from "react";
import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
import Switch from "@mui/material/Switch";
import Toolbar from "@mui/material/Toolbar";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";
import { Link, NavLink, useLocation } from "react-router-dom";
import { BookOpen, Coins, LayoutGrid, Moon, Settings as SettingsIcon, Sun } from "lucide-react";
import { usePreflight } from "../lib/PreflightContext";
import { useColorMode } from "../theme";
import forgeIcon from "../../../../../../assets/icon.png";

const NAV = [
  { to: "/", label: "Runs", icon: LayoutGrid, end: true },
  { to: "/costs", label: "Costs", icon: Coins, end: false },
  { to: "/docs", label: "Docs", icon: BookOpen, end: false },
  { to: "/settings", label: "Settings", icon: SettingsIcon, end: false },
];

export function Layout({ children }: { children: ReactNode }) {
  const location = useLocation();
  const theme = useTheme();
  const { mode, toggleMode } = useColorMode();
  const { preflight } = usePreflight();
  const notReady = preflight && !preflight.ready;

  return (
    <Box className="min-h-screen">
      <AppBar
        position="sticky"
        color="inherit"
        elevation={0}
        sx={{ borderBottom: 1, borderColor: "divider", bgcolor: "background.paper" }}
      >
        <Toolbar disableGutters sx={{ minHeight: "52px !important", px: { xs: 2, sm: 4 } }}>
          <Box
            sx={{
              width: "100%",
              maxWidth: 1280,
              mx: "auto",
              display: "flex",
              alignItems: "stretch",
              minHeight: 52,
            }}
          >
            <Box
              component={Link}
              to="/"
              sx={{
                display: "flex",
                alignItems: "center",
                gap: 1,
                pr: 3,
                color: "text.primary",
                textDecoration: "none",
              }}
            >
              <Box component="img" src={forgeIcon} alt="" aria-hidden sx={{ width: 30, height: 30, objectFit: "contain" }} />
              <Typography component="span" sx={{ fontSize: 15, fontWeight: 650, letterSpacing: "-0.01em" }}>
                ProgramSmith
              </Typography>
            </Box>

            <Box component="nav" sx={{ display: { xs: "none", sm: "flex" }, alignItems: "stretch" }}>
              {NAV.map(({ to, label, icon: Icon, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  style={({ isActive }) => ({
                    display: "flex",
                    alignItems: "center",
                    gap: 7,
                    padding: "0 14px",
                    borderBottom: `2px solid ${isActive ? theme.palette.primary.main : "transparent"}`,
                    color: isActive ? theme.palette.text.primary : theme.palette.text.secondary,
                    fontSize: 13,
                    fontWeight: isActive ? 600 : 500,
                    textDecoration: "none",
                  })}
                >
                  <Icon size={15} />
                  {label}
                </NavLink>
              ))}
            </Box>

            <Box sx={{ ml: "auto", display: "flex", alignItems: "center", gap: 2 }}>
              <Tooltip title={`Switch to ${mode === "dark" ? "light" : "dark"} mode`}>
                <Box sx={{ display: "flex", alignItems: "center", gap: 0.5, color: "text.secondary" }}>
                  <Moon size={14} aria-hidden="true" />
                  <Switch
                    checked={mode === "light"}
                    onChange={toggleMode}
                    size="small"
                    slotProps={{ input: { "aria-label": "Toggle light mode" } }}
                  />
                  <Sun size={14} aria-hidden="true" />
                </Box>
              </Tooltip>
              {notReady && location.pathname !== "/settings" && (
                <Box
                  component={Link}
                  to="/settings"
                  sx={{
                    border: 1,
                    borderColor: "warning.main",
                    color: "warning.main",
                    px: 1.5,
                    py: 0.75,
                    fontSize: 12,
                    fontWeight: 600,
                    textDecoration: "none",
                  }}
                >
                  Setup incomplete
                </Box>
              )}
            </Box>
          </Box>
        </Toolbar>
      </AppBar>

      <Box component="main" sx={{ maxWidth: 1280, mx: "auto", px: { xs: 2, sm: 4 }, pt: 4, pb: 10 }}>
        {children}
      </Box>
    </Box>
  );
}
