import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Paper,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useInfiniteQuery } from "@tanstack/react-query";
import { type GridColDef } from "@mui/x-data-grid";
import { useState, type FormEvent } from "react";
import { api, errorMessage } from "../../api/client";
import type { AuditLogEntry } from "../../api/types";
import { DataGridShell } from "../../components/DataGridShell";
import { formatDateTime, toIsoDateTime } from "../../format";
import { AuditDetailDrawer } from "./AuditDetailDrawer";

interface AuditFilters {
  actor_id?: string;
  action?: string;
  target_type?: string;
  target_id?: string;
  since?: string;
  until?: string;
}

export function AuditLogPage() {
  const [draftActorId, setDraftActorId] = useState("");
  const [draftAction, setDraftAction] = useState("");
  const [draftTargetType, setDraftTargetType] = useState("");
  const [draftTargetId, setDraftTargetId] = useState("");
  const [draftSince, setDraftSince] = useState("");
  const [draftUntil, setDraftUntil] = useState("");
  const [filters, setFilters] = useState<AuditFilters>({});
  const [selected, setSelected] = useState<AuditLogEntry | null>(null);

  const query = useInfiniteQuery({
    queryKey: ["admin-audit-log", filters],
    queryFn: ({ pageParam }) =>
      api.listAuditLog({
        actor_id: filters.actor_id,
        action: filters.action,
        target_type: filters.target_type,
        target_id: filters.target_id,
        since: filters.since,
        until: filters.until,
        cursor: pageParam,
        limit: 100,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? lastPage.next_cursor ?? undefined : undefined,
  });

  const rows = query.data?.pages.flatMap((page) => page.data) ?? [];

  const columns: GridColDef<AuditLogEntry>[] = [
    {
      field: "at",
      headerName: "Recorded at",
      width: 175,
      renderCell: (params) => formatDateTime(params.row.at),
    },
    { field: "action", headerName: "Action", minWidth: 180, flex: 1 },
    { field: "actor_role", headerName: "Actor role", width: 120 },
    {
      field: "actor_id",
      headerName: "Actor ID",
      minWidth: 190,
      flex: 1,
      renderCell: (params) => (
        <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: 12 }}>
          {params.row.actor_id ?? "—"}
        </Typography>
      ),
    },
    { field: "target_type", headerName: "Target type", width: 130 },
    {
      field: "target_id",
      headerName: "Target ID",
      minWidth: 190,
      flex: 1,
      renderCell: (params) => (
        <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: 12 }}>
          {params.row.target_id ?? "—"}
        </Typography>
      ),
    },
    { field: "ip", headerName: "IP", width: 140 },
  ];

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFilters({
      actor_id: draftActorId.trim() || undefined,
      action: draftAction.trim() || undefined,
      target_type: draftTargetType.trim() || undefined,
      target_id: draftTargetId.trim() || undefined,
      since: toIsoDateTime(draftSince),
      until: toIsoDateTime(draftUntil),
    });
  };

  const clearFilters = () => {
    setDraftActorId("");
    setDraftAction("");
    setDraftTargetType("");
    setDraftTargetId("");
    setDraftSince("");
    setDraftUntil("");
    setFilters({});
  };

  return (
    <Box>
      <Box sx={{ display: "flex", alignItems: "baseline", gap: 2, mb: 2 }}>
        <Typography variant="h1">Audit log</Typography>
        <Typography variant="body2" color="text.secondary">
          {rows.length} loaded
        </Typography>
      </Box>
      <Typography variant="body1" color="text.secondary" sx={{ mb: 2 }}>
        Read-only. The trail is append-only; hashes are shown truncated when you open a row.
      </Typography>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Box
          component="form"
          onSubmit={applyFilters}
          sx={{
            display: "flex",
            flexWrap: "wrap",
            gap: 2,
            alignItems: "center",
          }}
        >
          <TextField
            label="Actor ID"
            value={draftActorId}
            onChange={(event) => setDraftActorId(event.target.value)}
            size="small"
            sx={{ minWidth: 230 }}
            placeholder="UUID"
          />
          <TextField
            label="Action"
            value={draftAction}
            onChange={(event) => setDraftAction(event.target.value)}
            size="small"
            sx={{ minWidth: 170 }}
          />
          <TextField
            label="Target type"
            value={draftTargetType}
            onChange={(event) => setDraftTargetType(event.target.value)}
            size="small"
            sx={{ minWidth: 150 }}
          />
          <TextField
            label="Target ID"
            value={draftTargetId}
            onChange={(event) => setDraftTargetId(event.target.value)}
            size="small"
            sx={{ minWidth: 220 }}
            placeholder="UUID"
          />
          <TextField
            label="Since"
            type="datetime-local"
            value={draftSince}
            onChange={(event) => setDraftSince(event.target.value)}
            size="small"
            slotProps={{ inputLabel: { shrink: true } }}
          />
          <TextField
            label="Until"
            type="datetime-local"
            value={draftUntil}
            onChange={(event) => setDraftUntil(event.target.value)}
            size="small"
            slotProps={{ inputLabel: { shrink: true } }}
          />
          <Button type="submit" variant="contained">
            Search
          </Button>
          <Button onClick={clearFilters}>Clear</Button>
        </Box>
      </Paper>

      {query.isError ? (
        <Alert severity="error" sx={{ mb: 2 }}>
          {errorMessage(query.error)}
        </Alert>
      ) : null}

      <DataGridShell
        rows={rows}
        columns={columns}
        loading={query.isLoading || query.isFetching}
        emptyMessage="No audit entries match these filters."
        getRowId={(row) => row.id}
        onRowClick={(params) => setSelected(params.row)}
      />

      <Stack direction="row" spacing={2} sx={{ mt: 2, alignItems: "center" }}>
        {query.hasNextPage ? (
          <Button
            variant="outlined"
            onClick={() => void query.fetchNextPage()}
            disabled={query.isFetchingNextPage}
          >
            {query.isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        ) : (
          <Typography variant="body2" color="text.secondary">
            {rows.length > 0 ? "All matching entries are loaded." : ""}
          </Typography>
        )}
        {query.isFetching && !query.isLoading ? (
          <CircularProgress size={18} aria-label="Refreshing audit entries" />
        ) : null}
      </Stack>

      <AuditDetailDrawer
        entry={selected}
        open={selected !== null}
        onClose={() => setSelected(null)}
      />
    </Box>
  );
}
