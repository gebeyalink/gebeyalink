import {
  Alert,
  Box,
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  ListItemText,
  MenuItem,
  OutlinedInput,
  Select,
  Snackbar,
  TextField,
  Typography,
  type SelectChangeEvent,
} from "@mui/material";
import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, errorMessage } from "../../api/client";
import {
  ADDITIONAL_ROLES,
  PRIMARY_ROLES,
  type AccountStatus,
  type AdditionalRole,
  type AdminUser,
  type AdminUserPatchBody,
  type PrimaryRole,
} from "../../api/types";
import { ConfirmDialog } from "../../components/ConfirmDialog";

interface UserPatchDialogProps {
  user: AdminUser | null;
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}

const ADMIN_SETTABLE_STATUSES: AccountStatus[] = ["ACTIVE", "FROZEN", "SUSPENDED"];

export function UserPatchDialog({ user, open, onClose, onSaved }: UserPatchDialogProps) {
  const [status, setStatus] = useState<AccountStatus | "">("");
  const [role, setRole] = useState<PrimaryRole | "">("");
  const [rolesToAdd, setRolesToAdd] = useState<AdditionalRole[]>([]);
  const [rolesToRemove, setRolesToRemove] = useState<AdditionalRole[]>([]);
  const [reason, setReason] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [errorToast, setErrorToast] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (body: AdminUserPatchBody) => {
      if (!user) {
        throw new Error("No user selected.");
      }
      return api.patchUser(user.id, body);
    },
    onSuccess: () => {
      setConfirmOpen(false);
      onSaved();
    },
    onError: (error) => {
      setConfirmOpen(false);
      setErrorToast(errorMessage(error));
    },
  });

  useEffect(() => {
    if (!open) {
      return;
    }
    setStatus("");
    setRole("");
    setRolesToAdd([]);
    setRolesToRemove([]);
    setReason("");
    setConfirmOpen(false);
    setErrorToast(null);
    mutation.reset();
    // The reset is tied to opening a row, not to a mutation object identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, user?.id]);

  if (!user) {
    return null;
  }

  const availableToAdd = ADDITIONAL_ROLES.filter(
    (additionalRole) => !user.additional_roles.includes(additionalRole),
  );
  const availableToRemove = user.additional_roles;
  const reasonMissing = status === "FROZEN" && reason.trim().length === 0;
  const hasChanges =
    status !== "" ||
    role !== "" ||
    rolesToAdd.length > 0 ||
    rolesToRemove.length > 0;

  const handleSelectRoles = (
    event: SelectChangeEvent<AdditionalRole[]>,
    setter: (value: AdditionalRole[]) => void,
  ) => {
    const value = event.target.value;
    setter((Array.isArray(value) ? value : value.split(",")) as AdditionalRole[]);
  };

  const handleRequestSave = () => {
    mutation.reset();
    setConfirmOpen(true);
  };

  const handleConfirm = () => {
    const body: AdminUserPatchBody = {};
    if (status) {
      body.status = status;
    }
    if (role) {
      body.role = role;
    }
    if (rolesToAdd.length > 0) {
      body.additional_roles_add = rolesToAdd;
    }
    if (rolesToRemove.length > 0) {
      body.additional_roles_remove = rolesToRemove;
    }
    if (reason.trim()) {
      body.reason = reason.trim();
    }
    mutation.mutate(body);
  };

  return (
    <>
      <Dialog
        open={open}
        onClose={mutation.isPending ? undefined : onClose}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>Edit {user.phone_e164}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            Leave a field unchanged unless you mean to change it. FROZEN requires a reason.
          </Typography>

          {mutation.isError ? (
            <Alert severity="error" sx={{ my: 2 }}>
              {errorMessage(mutation.error)}
            </Alert>
          ) : null}

          <TextField
            select
            fullWidth
            margin="normal"
            label="Status"
            value={status}
            onChange={(event) => setStatus(event.target.value as AccountStatus | "")}
            helperText={`Current: ${user.status}`}
          >
            <MenuItem value="">No change</MenuItem>
            {ADMIN_SETTABLE_STATUSES.map((option) => (
              <MenuItem key={option} value={option}>
                {option}
              </MenuItem>
            ))}
          </TextField>

          <TextField
            select
            fullWidth
            margin="normal"
            label="Primary role"
            value={role}
            onChange={(event) => setRole(event.target.value as PrimaryRole | "")}
            helperText={`Current: ${user.role}`}
          >
            <MenuItem value="">No change</MenuItem>
            {PRIMARY_ROLES.map((option) => (
              <MenuItem key={option} value={option}>
                {option}
              </MenuItem>
            ))}
          </TextField>

          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" },
              gap: 2,
              mt: 2,
            }}
          >
            <FormControl fullWidth disabled={availableToAdd.length === 0}>
              <InputLabel id="add-roles-label">Add additional roles</InputLabel>
              <Select<AdditionalRole[]>
                labelId="add-roles-label"
                multiple
                value={rolesToAdd}
                onChange={(event) => handleSelectRoles(event, setRolesToAdd)}
                input={<OutlinedInput label="Add additional roles" />}
                renderValue={(selected) =>
                  selected.length > 0 ? selected.join(", ") : "None"
                }
              >
                {availableToAdd.map((additionalRole) => (
                  <MenuItem key={additionalRole} value={additionalRole}>
                    <Checkbox checked={rolesToAdd.includes(additionalRole)} />
                    <ListItemText primary={additionalRole} />
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            <FormControl fullWidth disabled={availableToRemove.length === 0}>
              <InputLabel id="remove-roles-label">Remove additional roles</InputLabel>
              <Select<AdditionalRole[]>
                labelId="remove-roles-label"
                multiple
                value={rolesToRemove}
                onChange={(event) => handleSelectRoles(event, setRolesToRemove)}
                input={<OutlinedInput label="Remove additional roles" />}
                renderValue={(selected) =>
                  selected.length > 0 ? selected.join(", ") : "None"
                }
              >
                {availableToRemove.map((additionalRole) => (
                  <MenuItem key={additionalRole} value={additionalRole}>
                    <Checkbox checked={rolesToRemove.includes(additionalRole)} />
                    <ListItemText primary={additionalRole} />
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Box>

          <TextField
            fullWidth
            multiline
            minRows={2}
            margin="normal"
            label="Reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            required={status === "FROZEN"}
            error={reasonMissing}
            helperText={
              reasonMissing
                ? "A reason is required when the status becomes FROZEN."
                : "Kept with the audit record. Maximum 500 characters."
            }
            slotProps={{ htmlInput: { maxLength: 500 } }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button
            onClick={handleRequestSave}
            variant="contained"
            disabled={!hasChanges || reasonMissing || mutation.isPending}
          >
            Save changes
          </Button>
        </DialogActions>
      </Dialog>

      <ConfirmDialog
        open={confirmOpen}
        title="Confirm account change"
        description={`Apply these changes to ${user.phone_e164}?`}
        confirmLabel="Save changes"
        loading={mutation.isPending}
        onClose={() => setConfirmOpen(false)}
        onConfirm={handleConfirm}
      />

      <Snackbar
        open={errorToast !== null}
        autoHideDuration={6000}
        onClose={() => setErrorToast(null)}
      >
        <Alert severity="error" variant="filled" onClose={() => setErrorToast(null)}>
          {errorToast ?? ""}
        </Alert>
      </Snackbar>
    </>
  );
}
