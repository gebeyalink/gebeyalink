import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Snackbar,
  TextField,
  Typography,
} from "@mui/material";
import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { ListingAdmin } from "../../api/types";
import { AUDIT_NOTICE } from "../../components/ConfirmDialog";

interface SuspendDialogProps {
  listing: ListingAdmin | null;
  open: boolean;
  onClose: () => void;
  onSuspended: () => void;
}

export function SuspendDialog({ listing, open, onClose, onSuspended }: SuspendDialogProps) {
  const [reason, setReason] = useState("");
  const [errorToast, setErrorToast] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (target: ListingAdmin) => api.suspendListing(target.id, reason.trim()),
    onSuccess: () => {
      onSuspended();
    },
    onError: (error) => {
      setErrorToast(errorMessage(error));
    },
  });

  useEffect(() => {
    if (open) {
      setReason("");
      setErrorToast(null);
      mutation.reset();
    }
    // Reset the form for each row; the mutation object is intentionally not a dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, listing?.id]);

  if (!listing) {
    return null;
  }

  const handleSubmit = () => {
    if (!reason.trim()) {
      return;
    }
    mutation.mutate(listing);
  };

  return (
    <>
      <Dialog
        open={open}
        onClose={mutation.isPending ? undefined : onClose}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>Suspend {listing.short_code}</DialogTitle>
        <DialogContent>
          <Typography variant="body1" sx={{ mb: 2 }}>
            Suspending removes this listing from buyer search until it is restored.
          </Typography>
          <Alert severity="info" sx={{ mb: 2 }}>
            {AUDIT_NOTICE}
          </Alert>
          {mutation.isError ? (
            <Alert severity="error" sx={{ mb: 2 }}>
              {errorMessage(mutation.error)}
            </Alert>
          ) : null}
          <TextField
            label="Reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            required
            fullWidth
            multiline
            minRows={3}
            autoFocus
            helperText="Required. Kept with the audit record. Maximum 500 characters."
            slotProps={{ htmlInput: { maxLength: 500 } }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            color="error"
            variant="contained"
            disabled={mutation.isPending || reason.trim().length === 0}
          >
            {mutation.isPending ? "Suspending…" : "Suspend listing"}
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={errorToast !== null}
        autoHideDuration={5000}
        onClose={() => setErrorToast(null)}
      >
        <Alert severity="error" variant="filled" onClose={() => setErrorToast(null)}>
          {errorToast ?? ""}
        </Alert>
      </Snackbar>
    </>
  );
}
