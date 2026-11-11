import type {
  AdminUser,
  AdminUserPatchBody,
  AuditLogEntry,
  CursorPage,
  ErrorBody,
  ListingAdmin,
  ListingModerationResponse,
  ListingStatus,
  RegionCode,
  StaffLoginBody,
  StaffPasswordChangeBody,
  StaffTokenResponse,
} from "./types";

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "/v1").replace(/\/$/, "");
const SESSION_KEY = "gebeyalink.admin.session";

export const AUTH_CHANGED_EVENT = "gebeyalink:auth-changed";

export interface StaffSession {
  accessToken: string;
  expiresAt: string;
  scope: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly traceId: string | null;

  constructor(
    message: string,
    status: number,
    code = "REQUEST_FAILED",
    details: Record<string, unknown> = {},
    traceId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.traceId = traceId;
  }
}

function notifyAuthChanged(): void {
  window.dispatchEvent(new Event(AUTH_CHANGED_EVENT));
}

export function getSession(): StaffSession | null {
  const raw = window.localStorage.getItem(SESSION_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as Partial<StaffSession>;
    if (!parsed.accessToken || !parsed.expiresAt || !parsed.scope) {
      return null;
    }
    return {
      accessToken: parsed.accessToken,
      expiresAt: parsed.expiresAt,
      scope: parsed.scope,
    };
  } catch {
    return null;
  }
}

export function saveSession(response: StaffTokenResponse): StaffSession {
  const session: StaffSession = {
    accessToken: response.access_token,
    expiresAt: response.expires_at,
    scope: response.scope,
  };
  window.localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  notifyAuthChanged();
  return session;
}

export function clearSession(): void {
  window.localStorage.removeItem(SESSION_KEY);
  notifyAuthChanged();
}

function isErrorBody(value: unknown): value is ErrorBody {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const error = (value as { error?: unknown }).error;
  return typeof error === "object" && error !== null && "message" in error;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const session = getSession();
  if (session) {
    headers.set("Authorization", `Bearer ${session.accessToken}`);
  }

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, { ...init, headers });
  } catch {
    throw new ApiError("The API could not be reached.", 0, "NETWORK_ERROR");
  }

  if (response.status === 401) {
    clearSession();
    throw new ApiError("Your session has expired. Sign in again.", 401, "TOKEN_INVALID");
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    if (isErrorBody(payload)) {
      throw new ApiError(
        payload.error.message,
        response.status,
        payload.error.code,
        payload.error.details,
        payload.error.trace_id,
      );
    }
    throw new ApiError(response.statusText || "The request failed.", response.status);
  }

  return payload as T;
}

function query(params: object): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (typeof value === "string" || typeof value === "number") {
      if (value !== "") {
        search.set(key, String(value));
      }
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.code === "REQUEST_FAILED" ? error.message : `${error.message} (${error.code})`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "An unexpected error occurred.";
}

export interface UserSearchParams {
  phone_e164?: string;
  status?: string;
  cursor?: string;
  limit?: number;
}

export interface ListingSearchParams {
  seller_id?: string;
  status?: ListingStatus;
  crop_id?: number;
  region_code?: RegionCode;
  q?: string;
  cursor?: string;
  limit?: number;
}

export interface AuditSearchParams {
  actor_id?: string;
  action?: string;
  target_type?: string;
  target_id?: string;
  since?: string;
  until?: string;
  cursor?: string;
  limit?: number;
}

export const api = {
  login: (body: StaffLoginBody) =>
    request<StaffTokenResponse>("/admin/login", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  changePassword: (body: StaffPasswordChangeBody) =>
    request<void>("/admin/change-password", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  logout: () =>
    request<void>("/admin/logout", {
      method: "POST",
    }),

  listUsers: (params: UserSearchParams = {}) =>
    request<CursorPage<AdminUser>>(`/admin/users${query(params)}`),

  patchUser: (id: string, body: AdminUserPatchBody) =>
    request<AdminUser>(`/admin/users/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  listListings: (params: ListingSearchParams = {}) =>
    request<CursorPage<ListingAdmin>>(`/admin/listings${query(params)}`),

  suspendListing: (id: string, reason: string) =>
    request<ListingModerationResponse>(`/admin/listings/${encodeURIComponent(id)}/suspend`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),

  restoreListing: (id: string) =>
    request<ListingModerationResponse>(`/admin/listings/${encodeURIComponent(id)}/restore`, {
      method: "POST",
    }),

  listAuditLog: (params: AuditSearchParams = {}) =>
    request<CursorPage<AuditLogEntry>>(`/admin/audit-log${query(params)}`),
};
