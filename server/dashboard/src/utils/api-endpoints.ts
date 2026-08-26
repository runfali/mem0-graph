export const AUTH_ENDPOINTS = {
  SETUP_STATUS: "/auth/setup-status",
  REGISTER: "/auth/register",
  LOGIN: "/auth/login",
  REFRESH: "/auth/refresh",
  ME: "/auth/me",
  CHANGE_PASSWORD: "/auth/change-password",
  REVOKE_REFRESH: "/auth/revoke-refresh",
  ONBOARDING_COMPLETE: "/auth/onboarding-complete",
} as const;

export const MEMORY_ENDPOINTS = {
  BASE: "/memories",
  SEARCH: "/memories/search",
  TYPES_DISTRIBUTION: "/memories/types-distribution",
  BY_ID: (memoryId: string) => `/memories/${memoryId}`,
  HISTORY: (memoryId: string) => `/memories/${memoryId}/history`,
  CONFIGURE: "/configure",
  CONFIGURE_PROVIDERS: "/configure/providers",
  RESET: "/reset",
  GENERATE_INSTRUCTIONS: "/generate-instructions",
} as const;

export const API_KEY_ENDPOINTS = {
  BASE: "/api-keys",
  BY_ID: (keyId: string) => `/api-keys/${keyId}`,
} as const;

export const REQUEST_ENDPOINTS = {
  BASE: "/requests",
} as const;

export const ENTITY_ENDPOINTS = {
  BASE: "/entities",
  BY_ID: (type: string, id: string) =>
    `/entities/${type}/${encodeURIComponent(id)}`,
} as const;

export const EVOLVE_ENDPOINTS = {
  REPORT: "/evolve/report",
  RETAIN: (memoryId: string) =>
    `/evolve/memory/${encodeURIComponent(memoryId)}/retain`,
} as const;

export const REFINE_ENDPOINTS = {
  CANDIDATES: "/memory/refine/candidates",
  APPLY: "/memory/refine/apply",
  ROLLBACK: "/memory/refine/rollback",
  HISTORY: "/memory/refine/history",
} as const;

export const SEARCH_KEYWORDS_ENDPOINTS = {
  BASE: "/search-keywords",
  BY_ID: (id: number) => `/search-keywords/${id}`,
} as const;

export const SEARCH_ENDPOINTS = {
  BASE: "/search",
} as const;
