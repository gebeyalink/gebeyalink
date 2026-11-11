import { Alert, Box, Button, Paper, Stack, TextField, Typography } from "@mui/material";
import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, clearSession, errorMessage } from "../../api/client";
import { ConfirmDialog } from "../../components/ConfirmDialog";

export function ChangePasswordPage() {
  const navigate = useNavigate();
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);

  const change = useMutation({
    mutationFn: api.changePassword,
    onSuccess: () => {
      clearSession();
      navigate("/login", {
        replace: true,
        state: {
          notice: "Password changed. Sign in with the new password and a fresh code.",
        },
      });
    },
    onError: () => {
      setConfirmOpen(false);
    },
  });

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setConfirmOpen(true);
  };

  const handleConfirm = () => {
    change.mutate({
      old_password: oldPassword,
      new_password: newPassword,
      totp_code: totpCode.trim(),
    });
  };

  const cancel = () => {
    clearSession();
    navigate("/login", { replace: true });
  };

  return (
    <Box sx={{ minHeight: "100vh", display: "grid", placeItems: "center", p: 2 }}>
      <Paper elevation={3} sx={{ width: "100%", maxWidth: 520, p: { xs: 3, sm: 4 } }}>
        <Typography variant="h1" gutterBottom>
          Choose a new password
        </Typography>
        <Typography variant="body1" color="text.secondary" sx={{ mb: 3 }}>
          Your password is provisional. Replace it before the console will open. The change
          ends this session, so you will sign in again afterwards.
        </Typography>

        {change.isError ? (
          <Alert severity="error" sx={{ mb: 2 }}>
            {errorMessage(change.error)}
          </Alert>
        ) : null}

        <Box component="form" onSubmit={handleSubmit} noValidate>
          <TextField
            label="Current password"
            type="password"
            value={oldPassword}
            onChange={(event) => setOldPassword(event.target.value)}
            autoComplete="current-password"
            required
            fullWidth
            margin="normal"
          />
          <TextField
            label="New password"
            type="password"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
            autoComplete="new-password"
            required
            fullWidth
            margin="normal"
            helperText="At least twelve characters."
            slotProps={{ htmlInput: { minLength: 12 } }}
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
          />
          <Stack direction="row" spacing={2} sx={{ mt: 2 }}>
            <Button
              type="submit"
              variant="contained"
              size="large"
              disabled={change.isPending}
            >
              {change.isPending ? "Changing…" : "Change password"}
            </Button>
            <Button onClick={cancel} disabled={change.isPending}>
              Cancel and sign out
            </Button>
          </Stack>
        </Box>
      </Paper>

      <ConfirmDialog
        open={confirmOpen}
        title="Change password"
        description="Replace your current password now? This ends the current session."
        confirmLabel="Change password"
        loading={change.isPending}
        onClose={() => setConfirmOpen(false)}
        onConfirm={handleConfirm}
      />
    </Box>
  );
}
