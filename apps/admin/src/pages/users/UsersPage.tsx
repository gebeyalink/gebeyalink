import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  MenuItem,
  Paper,
  Snackbar,
  Stack,
  TextField,
  Typography,
  type ChipProps,
} from "@mui/material";
import EditIcon from "@mui/icons-material/Edit";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { GridActionsCellItem, type GridColDef } from "@mui/x-data-grid";
import { useState, type FormEvent } from "react";
import { api, errorMessage } from "../../api/client";
import {
  ACCOUNT_STATUSES,
  type AccountStatus,
  type AdminUser,
} from "../../api/types";
import { DataGridShell } from "../../components/DataGridShell";
import { formatDateTime } from "../../format";
import { UserPatchDialog } from "./UserPatchDialog";

interface UserFilters {
  phone_e164?: string;
  status?: AccountStatus;
}

type ChipColor = NonNullable<ChipProps["color"]>;

function statusColor(status: AccountStatus): ChipColor {
  switch (status) {
    case "ACTIVE":
      return "success";
    case "FROZEN":
    case "SUSPENDED":
      return "error";
    case "RECOVERY":
    case "COOLDOWN":
      return "warning";
    default:
      return "default";
  }
}

export function UsersPage() {
  const queryClient = useQueryClient();
  const [draftPhone, setDraftPhone] = useState("");
  const [draftStatus, setDraftStatus] = useState<AccountStatus | "">("");
  const [filters, setFilters] = useState<UserFilters>({});
  const [editing, setEditing] = useState<AdminUser | null>(null);
  const [savedMessage, setSavedMessage] = useState<string | null>(null);

  const query = useInfiniteQuery({
    queryKey: ["admin-users", filters],
    queryFn: ({ pageParam }) =>
      api.listUsers({
        phone_e164: filters.phone_e164,
        status: filters.status,
        cursor: pageParam,
        limit: 100,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? lastPage.next_cursor ?? undefined : undefined,
  });

  const rows = query.data?.pages.flatMap((page) => page.data) ?? [];

  const columns: GridColDef<AdminUser>[] = [
    { field: "phone_e164", headerName: "Phone", minWidth: 155, flex: 1 },
    {
      field: "status",
      headerName: "Status",
      width: 125,
      renderCell: (params) => (
        <Chip
          size="small"
          label={params.row.status}
          color={statusColor(params.row.status)}
        />
      ),
    },
    { field: "role", headerName: "Primary role", width: 125 },
    {
      field: "additional_roles",
      headerName: "Additional roles",
      minWidth: 165,
      flex: 1,
      renderCell: (params) =>
        params.row.additional_roles.length > 0
          ? params.row.additional_roles.join(", ")
          : "—",
    },
    { field: "kyc_tier", headerName: "KYC tier", width: 100 },
    {
      field: "created_at",
      headerName: "Created",
      minWidth: 175,
      renderCell: (params) => formatDateTime(params.row.created_at),
    },
    {
      field: "actions",
      type: "actions",
      headerName: "",
      width: 80,
      getActions: (params) => [
        <GridActionsCellItem
          key="edit"
          icon={<EditIcon />}
          label="Edit account"
          onClick={() => setEditing(params.row)}
        />,
      ],
    },
  ];

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFilters({
      phone_e164: draftPhone.trim() || undefined,
      status: draftStatus || undefined,
    });
  };

  const clearFilters = () => {
    setDraftPhone("");
    setDraftStatus("");
    setFilters({});
  };

  const handleSaved = () => {
    setEditing(null);
    setSavedMessage("User updated.");
    void queryClient.invalidateQueries({ queryKey: ["admin-users"] });
  };

  return (
    <Box>
      <Box sx={{ display: "flex", alignItems: "baseline", gap: 2, mb: 2 }}>
        <Typography variant="h1">Users</Typography>
        <Typography variant="body2" color="text.secondary">
          {rows.length} loaded
        </Typography>
      </Box>
      <Typography variant="body1" color="text.secondary" sx={{ mb: 2 }}>
        Search by phone prefix or account status. The server enforces which role may make a
        change; a refused change is shown as an error.
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
            label="Phone prefix"
            value={draftPhone}
            onChange={(event) => setDraftPhone(event.target.value)}
            size="small"
            sx={{ minWidth: 200 }}
            placeholder="+2519…"
          />
          <TextField
            select
            label="Status"
            value={draftStatus}
            onChange={(event) => setDraftStatus(event.target.value as AccountStatus | "")}
            size="small"
            sx={{ minWidth: 180 }}
          >
            <MenuItem value="">Any status</MenuItem>
            {ACCOUNT_STATUSES.map((status) => (
              <MenuItem key={status} value={status}>
                {status}
              </MenuItem>
            ))}
          </TextField>
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
        emptyMessage="No accounts match these filters."
        getRowId={(row) => row.id}
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
            {rows.length > 0 ? "All matching accounts are loaded." : ""}
          </Typography>
        )}
        {query.isFetching && !query.isLoading ? (
          <CircularProgress size={18} aria-label="Refreshing accounts" />
        ) : null}
      </Stack>

      <UserPatchDialog
        user={editing}
        open={editing !== null}
        onClose={() => setEditing(null)}
        onSaved={handleSaved}
      />

      <Snackbar
        open={savedMessage !== null}
        autoHideDuration={4000}
        onClose={() => setSavedMessage(null)}
        message={savedMessage ?? ""}
      />
    </Box>
  );
}
