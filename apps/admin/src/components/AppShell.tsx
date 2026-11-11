import {
  AppBar,
  Box,
  Divider,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Toolbar,
  Typography,
  useMediaQuery,
  useTheme,
} from "@mui/material";
import GavelIcon from "@mui/icons-material/Gavel";
import LockResetIcon from "@mui/icons-material/LockReset";
import LogoutIcon from "@mui/icons-material/Logout";
import MenuIcon from "@mui/icons-material/Menu";
import PeopleIcon from "@mui/icons-material/People";
import ReceiptLongIcon from "@mui/icons-material/ReceiptLong";
import { useEffect, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { api, AUTH_CHANGED_EVENT, clearSession, getSession, type StaffSession } from "../api/client";
import { formatDateTime } from "../format";

const DRAWER_WIDTH = 240;

const NAV_ITEMS = [
  { label: "Users", path: "/users", icon: <PeopleIcon /> },
  { label: "Moderation", path: "/moderation", icon: <GavelIcon /> },
  { label: "Audit log", path: "/audit-log", icon: <ReceiptLongIcon /> },
  { label: "Change password", path: "/change-password", icon: <LockResetIcon /> },
];

export function AppShell() {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down("md"));
  const [mobileOpen, setMobileOpen] = useState(false);
  const [session, setSession] = useState<StaffSession | null>(() => getSession());
  const [now, setNow] = useState(() => Date.now());
  const [loggingOut, setLoggingOut] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    const syncSession = () => setSession(getSession());
    window.addEventListener(AUTH_CHANGED_EVENT, syncSession);
    return () => window.removeEventListener(AUTH_CHANGED_EVENT, syncSession);
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (session && new Date(session.expiresAt).getTime() <= now) {
      clearSession();
      navigate("/login", { replace: true });
    }
  }, [navigate, now, session]);

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await api.logout();
    } catch {
      // The local session is cleared regardless: a failed revoke must not trap staff in the UI.
    } finally {
      clearSession();
      setLoggingOut(false);
      navigate("/login", { replace: true });
    }
  };

  const drawer = (
    <Box role="navigation" aria-label="Staff console">
      <Toolbar sx={{ px: 2 }}>
        <Box>
          <Typography variant="h3" color="primary">
            GebeyaLink
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Staff console
          </Typography>
        </Box>
      </Toolbar>
      <Divider />
      <List>
        {NAV_ITEMS.map((item) => (
          <ListItemButton
            key={item.path}
            selected={location.pathname === item.path}
            onClick={() => {
              navigate(item.path);
              setMobileOpen(false);
            }}
          >
            <ListItemIcon>{item.icon}</ListItemIcon>
            <ListItemText primary={item.label} />
          </ListItemButton>
        ))}
      </List>
    </Box>
  );

  return (
    <Box sx={{ display: "flex", minHeight: "100vh" }}>
      <AppBar
        position="fixed"
        sx={{
          width: { md: `calc(100% - ${DRAWER_WIDTH}px)` },
          ml: { md: `${DRAWER_WIDTH}px` },
        }}
      >
        <Toolbar>
          {isMobile ? (
            <IconButton
              color="inherit"
              edge="start"
              onClick={() => setMobileOpen(true)}
              aria-label="Open navigation"
              sx={{ mr: 1 }}
            >
              <MenuIcon />
            </IconButton>
          ) : null}
          <Typography variant="h3" component="div" sx={{ flexGrow: 1 }}>
            {NAV_ITEMS.find((item) => item.path === location.pathname)?.label ?? "Staff console"}
          </Typography>
          <Typography variant="body2" sx={{ mr: 2, display: { xs: "none", sm: "block" } }}>
            {session ? `Session until ${formatDateTime(session.expiresAt)}` : "No active session"}
          </Typography>
          <IconButton
            color="inherit"
            onClick={handleLogout}
            disabled={loggingOut}
            aria-label="Sign out"
            title="Sign out"
          >
            <LogoutIcon />
          </IconButton>
        </Toolbar>
      </AppBar>

      <Box component="nav" sx={{ width: { md: DRAWER_WIDTH }, flexShrink: { md: 0 } }}>
        <Drawer
          variant={isMobile ? "temporary" : "permanent"}
          open={isMobile ? mobileOpen : true}
          onClose={() => setMobileOpen(false)}
          ModalProps={{ keepMounted: true }}
          sx={{
            "& .MuiDrawer-paper": {
              width: DRAWER_WIDTH,
              boxSizing: "border-box",
            },
          }}
        >
          {drawer}
        </Drawer>
      </Box>

      <Box
        component="main"
        sx={{
          flexGrow: 1,
          width: { md: `calc(100% - ${DRAWER_WIDTH}px)` },
          minWidth: 0,
        }}
      >
        <Toolbar />
        <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: 1600 }}>
          <Outlet />
        </Box>
      </Box>
    </Box>
  );
}
