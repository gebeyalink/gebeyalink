import { Alert, Box, Button, Paper, TextField, Typography } from "@mui/material";
import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api, errorMessage, saveSession } from "../../api/client";

interface LoginLocationState {
  notice?: string;
}

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const notice = (location.state as LoginLocationState | null)?.notice;
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");

  const login = useMutation({
    mutationFn: api.login,
    onSuccess: (response) => {
      saveSession(response);
      if (response.scope === "change_password_only" || response.must_change_password === true) {
        navigate("/change-password", { replace: true });
        return;
      }
      navigate("/users", { replace: true });
    },
  });

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    login.mutate({
      email: email.trim(),
      password,
      totp_code: totpCode.trim(),
    });
  };

  return (
    <Box
      sx={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        p: 2,
        background:
          "linear-gradient(135deg, rgba(27,94,32,0.12) 0%, rgba(244,247,243,1) 48%, rgba(239,108,0,0.10) 100%)",
      }}
    >
      <Paper elevation={3} sx={{ width: "100%", maxWidth: 440, p: { xs: 3, sm: 4 } }}>
        <Typography variant="h1" gutterBottom>
          GebeyaLink staff
        </Typography>
        <Typography variant="body1" color="text.secondary" sx={{ mb: 3 }}>
          Sign in with your password and the one-time code from your authenticator.
        </Typography>

        {notice ? (
          <Alert severity="success" sx={{ mb: 2 }}>
            {notice}
          </Alert>
        ) : null}
        {login.isError ? (
          <Alert severity="error" sx={{ mb: 2 }}>
            {errorMessage(login.error)}
          </Alert>
        ) : null}

        <Box component="form" onSubmit={handleSubmit} noValidate>
          <TextField
            label="Email"
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            autoComplete="username"
            required
            fullWidth
            margin="normal"
            autoFocus
          />
          <TextField
            label="Password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
            fullWidth
            margin="normal"
          />
          <TextField
            label="Authenticator code"
            value={totpCode}
            onChange={(event) => setTotpCode(event.target.value.replace(/\s/g, ""))}
            autoComplete="one-time-code"
            required
            fullWidth
            margin="normal"
            slotProps={{
              htmlInput: {
                inputMode: "numeric",
                pattern: "[0-9]*",
                minLength: 6,
                maxLength: 8,
              },
            }}
            helperText="Six to eight digits from your authenticator app."
          />
          <Button
            type="submit"
            variant="contained"
            size="large"
            fullWidth
            sx={{ mt: 2 }}
            disabled={login.isPending}
          >
            {login.isPending ? "Signing in…" : "Sign in"}
          </Button>
        </Box>

        <Typography variant="body2" color="text.secondary" sx={{ mt: 3 }}>
          Staff sessions last eight hours. A provisional password opens only the password-change
          screen.
        </Typography>
      </Paper>
    </Box>
  );
}
