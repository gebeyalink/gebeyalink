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
import BlockIcon from "@mui/icons-material/Block";
import RestoreIcon from "@mui/icons-material/Restore";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { GridActionsCellItem, type GridColDef } from "@mui/x-data-grid";
import { useState, type FormEvent } from "react";
import { api, errorMessage } from "../../api/client";
import {
  LISTING_STATUSES,
  REGION_CODES,
  type ListingAdmin,
  type ListingStatus,
  type RegionCode,
} from "../../api/types";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { DataGridShell } from "../../components/DataGridShell";
import { formatDateTime, formatSantim } from "../../format";
import { SuspendDialog } from "./SuspendDialog";

interface ListingFilters {
  status?: ListingStatus;
  seller_id?: string;
  crop_id?: number;
  region_code?: RegionCode;
  q?: string;
}

type ChipColor = NonNullable<ChipProps["color"]>;

function statusColor(status: ListingStatus): ChipColor {
  switch (status) {
    case "ACTIVE":
      return "success";
    case "SUSPENDED":
      return "error";
    case "DRAFT":
    case "PAUSED":
    case "EXPIRED":
      return "warning";
    default:
      return "default";
  }
}

interface ToastState {
  message: string;
  severity: "success" | "error";
}

export function ModerationPage() {
  const queryClient = useQueryClient();
  const [draftStatus, setDraftStatus] = useState<ListingStatus | "">("");
  const [draftSellerId, setDraftSellerId] = useState("");
  const [draftCropId, setDraftCropId] = useState("");
  const [draftRegion, setDraftRegion] = useState<RegionCode | "">("");
  const [draftQuery, setDraftQuery] = useState("");
  const [filters, setFilters] = useState<ListingFilters>({});
  const [suspendTarget, setSuspendTarget] = useState<ListingAdmin | null>(null);
  const [restoreTarget, setRestoreTarget] = useState<ListingAdmin | null>(null);
  const [toast, setToast] = useState<ToastState | null>(null);

  const query = useInfiniteQuery({
    queryKey: ["admin-listings", filters],
    queryFn: ({ pageParam }) =>
      api.listListings({
        status: filters.status,
        seller_id: filters.seller_id,
        crop_id: filters.crop_id,
        region_code: filters.region_code,
        q: filters.q,
        cursor: pageParam,
        limit: 100,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? lastPage.next_cursor ?? undefined : undefined,
  });

  const rows = query.data?.pages.flatMap((page) => page.data) ?? [];

  const restore = useMutation({
    mutationFn: (listing: ListingAdmin) => api.restoreListing(listing.id),
    onSuccess: () => {
      setRestoreTarget(null);
      setToast({ message: "Listing restored.", severity: "success" });
      void queryClient.invalidateQueries({ queryKey: ["admin-listings"] });
    },
    onError: (error) => {
      setToast({ message: errorMessage(error), severity: "error" });
    },
  });

  const handleSaved = () => {
    setSuspendTarget(null);
    setToast({ message: "Listing suspended.", severity: "success" });
    void queryClient.invalidateQueries({ queryKey: ["admin-listings"] });
  };

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const cropId = Number.parseInt(draftCropId, 10);
    setFilters({
      status: draftStatus || undefined,
      seller_id: draftSellerId.trim() || undefined,
      crop_id: Number.isFinite(cropId) && cropId > 0 ? cropId : undefined,
      region_code: draftRegion || undefined,
      q: draftQuery.trim() || undefined,
    });
  };

  const clearFilters = () => {
    setDraftStatus("");
    setDraftSellerId("");
    setDraftCropId("");
    setDraftRegion("");
    setDraftQuery("");
    setFilters({});
  };

  const columns: GridColDef<ListingAdmin>[] = [
    { field: "short_code", headerName: "Code", width: 95 },
    {
      field: "status",
      headerName: "Status",
      width: 120,
      renderCell: (params) => (
        <Chip size="small" label={params.row.status} color={statusColor(params.row.status)} />
      ),
    },
    {
      field: "quantity_kg",
      headerName: "Quantity (kg)",
      width: 125,
      type: "number",
    },
    {
      field: "available_kg",
      headerName: "Available (kg)",
      width: 125,
      type: "number",
    },
    {
      field: "unit_price_santim",
      headerName: "Unit price",
      width: 130,
      renderCell: (params) => formatSantim(params.row.unit_price_santim),
    },
    { field: "crop_id", headerName: "Crop", width: 85 },
    { field: "region_code", headerName: "Region", width: 90 },
    {
      field: "seller_id",
      headerName: "Seller",
      minWidth: 190,
      flex: 1,
      renderCell: (params) => (
        <Typography variant="body2" sx={{ fontFamily: "monospace" }}>
          {params.row.seller_id}
        </Typography>
      ),
    },
    {
      field: "created_at",
      headerName: "Created",
      width: 175,
      renderCell: (params) => formatDateTime(params.row.created_at),
    },
    {
      field: "actions",
      type: "actions",
      headerName: "",
      width: 95,
      getActions: (params) => {
        if (params.row.status === "ACTIVE") {
          return [
            <GridActionsCellItem
              key="suspend"
              icon={<BlockIcon color="error" />}
              label="Suspend listing"
              onClick={() => setSuspendTarget(params.row)}
            />,
          ];
        }
        if (params.row.status === "SUSPENDED") {
          return [
            <GridActionsCellItem
              key="restore"
              icon={<RestoreIcon color="success" />}
              label="Restore listing"
              onClick={() => setRestoreTarget(params.row)}
            />,
          ];
        }
        return [];
      },
    },
  ];

  return (
    <Box>
      <Box sx={{ display: "flex", alignItems: "baseline", gap: 2, mb: 2 }}>
        <Typography variant="h1">Moderation</Typography>
        <Typography variant="body2" color="text.secondary">
          {rows.length} loaded
        </Typography>
      </Box>
      <Typography variant="body1" color="text.secondary" sx={{ mb: 2 }}>
        Suspend an active listing with a reason, or restore a suspended one. The server checks
        the listing state before it acts.
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
            select
            label="Status"
            value={draftStatus}
            onChange={(event) => setDraftStatus(event.target.value as ListingStatus | "")}
            size="small"
            sx={{ minWidth: 160 }}
          >
            <MenuItem value="">Any status</MenuItem>
            {LISTING_STATUSES.map((status) => (
              <MenuItem key={status} value={status}>
                {status}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="Seller ID"
            value={draftSellerId}
            onChange={(event) => setDraftSellerId(event.target.value)}
            size="small"
            sx={{ minWidth: 250 }}
            placeholder="UUID"
          />
          <TextField
            label="Crop ID"
            value={draftCropId}
            onChange={(event) => setDraftCropId(event.target.value.replace(/[^0-9]/g, ""))}
            size="small"
            sx={{ width: 120 }}
            slotProps={{ htmlInput: { inputMode: "numeric" } }}
          />
          <TextField
            select
            label="Region"
            value={draftRegion}
            onChange={(event) => setDraftRegion(event.target.value as RegionCode | "")}
            size="small"
            sx={{ minWidth: 135 }}
          >
            <MenuItem value="">Any region</MenuItem>
            {REGION_CODES.map((region) => (
              <MenuItem key={region} value={region}>
                {region}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="Description"
            value={draftQuery}
            onChange={(event) => setDraftQuery(event.target.value)}
            size="small"
            sx={{ minWidth: 210 }}
            slotProps={{ htmlInput: { maxLength: 200 } }}
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
        emptyMessage="No listings match these filters."
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
            {rows.length > 0 ? "All matching listings are loaded." : ""}
          </Typography>
        )}
        {query.isFetching && !query.isLoading ? (
          <CircularProgress size={18} aria-label="Refreshing listings" />
        ) : null}
      </Stack>

      <SuspendDialog
        listing={suspendTarget}
        open={suspendTarget !== null}
        onClose={() => setSuspendTarget(null)}
        onSuspended={handleSaved}
      />

      <ConfirmDialog
        open={restoreTarget !== null}
        title="Restore listing"
        description={
          restoreTarget
            ? `Restore ${restoreTarget.short_code} to buyer search?`
            : undefined
        }
        confirmLabel="Restore listing"
        loading={restore.isPending}
        onClose={() => {
          if (!restore.isPending) {
            setRestoreTarget(null);
          }
        }}
        onConfirm={() => {
          if (restoreTarget) {
            restore.mutate(restoreTarget);
          }
        }}
      />

      <Snackbar
        open={toast !== null}
        autoHideDuration={5000}
        onClose={() => setToast(null)}
      >
        <Alert
          severity={toast?.severity ?? "info"}
          onClose={() => setToast(null)}
          variant="filled"
        >
          {toast?.message ?? ""}
        </Alert>
      </Snackbar>
    </Box>
  );
}
