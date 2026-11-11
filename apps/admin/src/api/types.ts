export const STAFF_ROLES = ["VERIFIER", "SUPPORT", "MODERATOR", "ADMIN"] as const;
export type StaffRole = (typeof STAFF_ROLES)[number];

export const ACCOUNT_STATUSES = [
  "ACTIVE",
  "RECOVERY",
  "COOLDOWN",
  "FROZEN",
  "SUSPENDED",
  "CLOSED",
] as const;
export type AccountStatus = (typeof ACCOUNT_STATUSES)[number];

export const PRIMARY_ROLES = ["FARMER", "BUYER"] as const;
export type PrimaryRole = (typeof PRIMARY_ROLES)[number];

export const ADDITIONAL_ROLES = ["AGENT", "COLLECTION_AGENT"] as const;
export type AdditionalRole = (typeof ADDITIONAL_ROLES)[number];

export const LISTING_STATUSES = [
  "DRAFT",
  "ACTIVE",
  "PAUSED",
  "SOLD_OUT",
  "EXPIRED",
  "SUSPENDED",
  "CLOSED",
] as const;
export type ListingStatus = (typeof LISTING_STATUSES)[number];

export const REGION_CODES = [
  "AA",
  "AF",
  "AM",
  "BE",
  "DD",
  "GA",
  "HA",
  "OR",
  "SI",
  "SO",
  "SW",
  "SN",
  "TI",
] as const;
export type RegionCode = (typeof REGION_CODES)[number];

export type VerificationTier = "TIER_0" | "TIER_1" | "TIER_2";

export interface CursorPage<T> {
  data: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface StaffTokenResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
  scope: string;
  /**
   * The served contract returns `scope`; some clients read `must_change_password`.
   * Keep it optional so the login screen can honour either without inventing a field.
   */
  must_change_password?: boolean;
}

export interface StaffLoginBody {
  email: string;
  password: string;
  totp_code: string;
}

export interface StaffPasswordChangeBody {
  old_password: string;
  new_password: string;
  totp_code: string;
}

export interface AdminUser {
  id: string;
  phone_e164: string;
  role: PrimaryRole;
  language: string;
  kyc_tier: VerificationTier;
  status: AccountStatus;
  additional_roles: AdditionalRole[];
  created_at: string;
  updated_at: string;
}

export interface AdminUserPatchBody {
  status?: AccountStatus;
  role?: PrimaryRole;
  additional_roles_add?: AdditionalRole[];
  additional_roles_remove?: AdditionalRole[];
  reason?: string;
}

export interface ListingAdmin {
  id: string;
  seller_id: string;
  crop_id: number;
  quantity_kg: number;
  available_kg: number;
  unit_price_santim: number;
  status: ListingStatus;
  region_code: string;
  short_code: string;
  created_at: string;
}

export interface ListingModerationResponse {
  id: string;
  status: ListingStatus;
}

export interface AuditLogEntry {
  id: string;
  actor_id: string | null;
  actor_role: string;
  action: string;
  target_type: string;
  target_id: string | null;
  before_hash: string | null;
  after_hash: string | null;
  ip: string | null;
  at: string;
}

export interface ErrorBody {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
    trace_id: string;
  };
}
