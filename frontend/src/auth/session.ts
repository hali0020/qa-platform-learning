import { computed, readonly, reactive } from "vue";
import {
  ApiError,
  qaApi,
  setCsrfCookieName,
  setUnauthorizedHandler,
} from "@/api";
import type {
  AuthStatus,
  CurrentUser,
  LoginRequest,
  SetupRequest,
} from "@/api";

interface AuthState {
  ready: boolean;
  loading: boolean;
  status: AuthStatus | null;
  user: CurrentUser | null;
}

const state = reactive<AuthState>({
  ready: false,
  loading: false,
  status: null,
  user: null,
});

let initialization: Promise<void> | null = null;

function markUnauthenticated(): void {
  state.user = null;
  if (state.status) state.status = { ...state.status, authenticated: false };
}

setUnauthorizedHandler(() => {
  markUnauthenticated();
  if (window.location.pathname === "/login") return;
  const requested = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  window.location.assign(`/login?redirect=${encodeURIComponent(requested)}`);
});

async function refresh(): Promise<void> {
  state.loading = true;
  try {
    const status = await qaApi.getAuthStatus();
    setCsrfCookieName(status.csrf_cookie_name);
    state.status = status;
    if (!state.status.enabled || !state.status.authenticated) {
      state.user = null;
    } else {
      state.user = null;
      try {
        state.user = await qaApi.getCurrentUser();
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          markUnauthenticated();
        } else {
          throw error;
        }
      }
    }
    state.ready = true;
  } catch (error) {
    state.ready = false;
    throw error;
  } finally {
    state.loading = false;
  }
}

async function initialize(): Promise<void> {
  if (state.ready) return;
  if (!initialization) {
    initialization = refresh().finally(() => {
      initialization = null;
    });
  }
  await initialization;
}

async function acceptAuthResult(
  action: () => ReturnType<typeof qaApi.login>,
): Promise<void> {
  state.loading = true;
  try {
    const result = await action();
    state.user = result.user;
    state.status = {
      enabled: true,
      authentication_method: state.status?.authentication_method ?? "local_accounts",
      setup_required: false,
      setup_allowed: false,
      authenticated: true,
      csrf_cookie_name: state.status?.csrf_cookie_name ?? "qa_csrf",
    };
    state.ready = true;
  } finally {
    state.loading = false;
  }
}

async function login(payload: LoginRequest): Promise<void> {
  await acceptAuthResult(() => qaApi.login(payload));
}

async function setup(payload: SetupRequest): Promise<void> {
  await acceptAuthResult(() => qaApi.setupAdmin(payload));
}

async function logout(): Promise<void> {
  try {
    await qaApi.logout();
  } finally {
    markUnauthenticated();
  }
}

function can(permission: string): boolean {
  if (state.status?.enabled === false) return true;
  return state.user?.permissions.includes(permission) ?? false;
}

function canAny(...permissions: string[]): boolean {
  return permissions.some(can);
}

export function useAuthSession() {
  return {
    state: readonly(state),
    isAuthenticated: computed(
      () => state.status?.enabled === false || state.user !== null,
    ),
    initialize,
    refresh,
    login,
    setup,
    logout,
    can,
    canAny,
  };
}
