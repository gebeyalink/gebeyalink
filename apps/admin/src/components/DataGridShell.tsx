import { Paper } from "@mui/material";
import {
  DataGrid,
  type GridColDef,
  type GridRowParams,
  type GridValidRowModel,
} from "@mui/x-data-grid";

interface DataGridShellProps<T extends GridValidRowModel> {
  rows: T[];
  columns: GridColDef<T>[];
  loading?: boolean;
  emptyMessage?: string;
  onRowClick?: (params: GridRowParams<T>) => void;
  getRowId?: (row: T) => string;
}

export function DataGridShell<T extends GridValidRowModel>({
  rows,
  columns,
  loading = false,
  emptyMessage = "Nothing to show.",
  onRowClick,
  getRowId,
}: DataGridShellProps<T>) {
  return (
    <Paper variant="outlined" sx={{ width: "100%", overflow: "hidden" }}>
      <DataGrid<T>
        rows={rows}
        columns={columns}
        loading={loading}
        autoHeight
        density="compact"
        disableRowSelectionOnClick
        onRowClick={onRowClick}
        getRowId={getRowId}
        pageSizeOptions={[25, 50, 100]}
        initialState={{
          pagination: {
            paginationModel: { page: 0, pageSize: 25 },
          },
        }}
        localeText={{
          noRowsLabel: emptyMessage,
        }}
        sx={{
          border: 0,
          minHeight: 360,
          "& .MuiDataGrid-columnHeaders": {
            backgroundColor: "rgba(27, 94, 32, 0.06)",
          },
        }}
      />
    </Paper>
  );
}
