import {
  Box,
  Divider,
  Drawer,
  IconButton,
  Stack,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import type { ReactNode } from "react";
import type { AuditLogEntry } from "../../api/types";
import { formatDateTime, truncateHash } from "../../format";

interface AuditDetailDrawerProps {
  entry: AuditLogEntry | null;
  open: boolean;
  onClose: () => void;
}

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Box>
      <Typography variant="body2" color="text.secondary">
        {label}
      </Typography>
      <Typography variant="body1" sx={{ wordBreak: "break-word" }}>
        {children}
      </Typography>
    </Box>
  );
}

export function AuditDetailDrawer({ entry, open, onClose }: AuditDetailDrawerProps) {
  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      sx={{
        "& .MuiDrawer-paper": {
          width: { xs: "100%", sm: 480 },
          p: 3,
          boxSizing: "border-box",
        },
      }}
    >
      <Box role="presentation">
        <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 2 }}>
          <Typography variant="h2">Audit entry</Typography>
          <IconButton onClick={onClose} aria-label="Close audit entry">
            <CloseIcon />
          </IconButton>
        </Stack>
        <Divider sx={{ mb: 2 }} />
        {entry ? (
          <Stack spacing={2}>
            <DetailRow label="Recorded at">{formatDateTime(entry.at)}</DetailRow>
            <DetailRow label="Action">{entry.action}</DetailRow>
            <DetailRow label="Actor role">{entry.actor_role}</DetailRow>
            <DetailRow label="Actor ID">
              <Box component="span" sx={{ fontFamily: "monospace" }}>
                {entry.actor_id ?? "—"}
              </Box>
            </DetailRow>
            <DetailRow label="Target">
              {entry.target_type}
              {entry.target_id ? (
                <Box
                  component="span"
                  sx={{ display: "block", fontFamily: "monospace", fontSize: 12 }}
                >
                  {entry.target_id}
                </Box>
              ) : null}
            </DetailRow>
            <DetailRow label="IP address">{entry.ip ?? "—"}</DetailRow>
            <DetailRow label="Before hash">
              <Box component="span" sx={{ fontFamily: "monospace", fontSize: 12 }}>
                {truncateHash(entry.before_hash, 40)}
              </Box>
            </DetailRow>
            <DetailRow label="After hash">
              <Box component="span" sx={{ fontFamily: "monospace", fontSize: 12 }}>
                {truncateHash(entry.after_hash, 40)}
              </Box>
            </DetailRow>
            <Typography variant="body2" color="text.secondary">
              Hash fragments are displayed truncated. The stored values are unchanged.
            </Typography>
          </Stack>
        ) : null}
      </Box>
    </Drawer>
  );
}
