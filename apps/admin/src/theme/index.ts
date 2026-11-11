import { createTheme } from "@mui/material/styles";

const theme = createTheme({
  spacing: 4,
  shape: {
    borderRadius: 10,
  },
  palette: {
    mode: "light",
    primary: {
      main: "#1B5E20",
      contrastText: "#FFFFFF",
    },
    secondary: {
      main: "#EF6C00",
    },
    background: {
      default: "#F4F7F3",
      paper: "#FFFFFF",
    },
    text: {
      primary: "#1A241B",
      secondary: "#526054",
    },
  },
  typography: {
    fontFamily:
      "'Noto Sans Ethiopic', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif",
    fontSize: 14,
    h1: { fontSize: 24, fontWeight: 700, lineHeight: 1.25 },
    h2: { fontSize: 20, fontWeight: 700, lineHeight: 1.3 },
    h3: { fontSize: 16, fontWeight: 700, lineHeight: 1.35 },
    body1: { fontSize: 14 },
    body2: { fontSize: 12 },
    button: { textTransform: "none", fontWeight: 600 },
  },
  components: {
    MuiButton: {
      defaultProps: {
        disableElevation: true,
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: "none",
        },
      },
    },
    MuiAppBar: {
      styleOverrides: {
        root: {
          boxShadow: "none",
        },
      },
    },
    MuiTableCell: {
      styleOverrides: {
        root: {
          fontSize: 13,
        },
      },
    },
  },
});

export default theme;
