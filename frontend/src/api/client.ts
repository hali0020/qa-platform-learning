import type { ApiEnvelope } from "./types";

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

function isLoopbackHost(hostname: string): boolean {
  return LOOPBACK_HOSTS.has(hostname) || /^127(?:\.\d{1,3}){3}$/.test(hostname);
}

function requireLocalPage(): void {
  if (!isLoopbackHost(window.location.hostname)) {
    throw new Error("教学前端只能从 localhost 或 127.0.0.1 打开");
  }
}

function resolveApiBaseUrl(configuredValue: string | undefined): string {
  const value = configuredValue?.trim().replace(/\/$/, "") ?? "";
  requireLocalPage();
  if (!value) return "";
  if (/[\\\u0000-\u001f\u007f]/.test(value)) {
    throw new Error("VITE_API_BASE_URL 不能包含反斜杠或控制字符");
  }

  let url: URL;
  try {
    url = new URL(value, window.location.origin);
  } catch {
    throw new Error("VITE_API_BASE_URL 必须是同源路径或有效的本机 URL");
  }
  if (!isLoopbackHost(url.hostname)) {
    throw new Error("教学前端只允许连接同源地址或本机回环地址");
  }
  if (!new Set(["http:", "https:"]).has(url.protocol)) {
    throw new Error("VITE_API_BASE_URL 只支持 HTTP(S)");
  }
  if (url.origin !== window.location.origin) {
    throw new Error("显式 API 地址必须与前端完全同源，避免越过本机反向代理");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("VITE_API_BASE_URL 不能包含凭据、查询参数或片段");
  }
  if (!value.startsWith("/") && url.pathname !== "/") {
    throw new Error("绝对 API 地址不能包含额外路径；请使用同源相对路径");
  }
  return value;
}

// 空字符串表示浏览器同源；页面与显式 API 地址都必须位于本机回环地址。
export const API_BASE_URL = resolveApiBaseUrl(import.meta.env.VITE_API_BASE_URL);
export const API_BASE_LABEL = API_BASE_URL || "same-origin /api";

type JsonRecord = Record<string, unknown>;

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const AUTH_ENTRY_PATHS = new Set([
  "/api/v1/auth/status",
  "/api/v1/auth/me",
  "/api/v1/auth/login",
  "/api/v1/auth/setup",
  "/api/v1/auth/change-password",
]);
let csrfCookieName = "qa_csrf";
let unauthorizedHandler: ((error: ApiError) => void) | undefined;

export function setCsrfCookieName(name: string): void {
  const normalized = name.trim();
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(normalized)) {
    throw new Error("后端返回了无效的 CSRF Cookie 名称");
  }
  csrfCookieName = normalized;
}

export function setUnauthorizedHandler(
  handler: ((error: ApiError) => void) | undefined,
): void {
  unauthorizedHandler = handler;
}

function cookieValue(name: string): string | undefined {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie
    .split(";")
    .map((value) => value.trim())
    .find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : undefined;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isApiEnvelope(value: unknown): value is ApiEnvelope<unknown> {
  return (
    isRecord(value) &&
    typeof value.code === "number" &&
    typeof value.message === "string" &&
    "data" in value
  );
}

function detailMessage(detail: unknown): string | undefined {
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return undefined;

  const messages = detail.flatMap((item) => {
    if (!isRecord(item) || typeof item.msg !== "string") return [];
    const location = Array.isArray(item.loc) ? item.loc.map(String).join(".") : "";
    return [location ? `${location}: ${item.msg}` : item.msg];
  });
  return messages.length ? messages.join("；") : undefined;
}

async function responsePayload(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code?: number,
    public readonly details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function errorFromResponse(response: Response, payload: unknown): ApiError {
  if (isApiEnvelope(payload)) {
    return new ApiError(payload.message, response.status, payload.code, payload.data);
  }
  if (isRecord(payload)) {
    const message = detailMessage(payload.detail);
    if (message) return new ApiError(message, response.status, undefined, payload.detail);
  }
  const fallback = typeof payload === "string" && payload ? payload : response.statusText;
  return new ApiError(fallback || `HTTP ${response.status}`, response.status, undefined, payload);
}

function requestHeaders(init: RequestInit): Headers {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  const method = (init.method ?? "GET").toUpperCase();
  const csrfToken = cookieValue(csrfCookieName);
  if (!SAFE_METHODS.has(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  if (
    init.body !== undefined &&
    !(init.body instanceof FormData) &&
    !(init.body instanceof Blob) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }
  return headers;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = 8_000,
): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const headers = requestHeaders(init);

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers,
      credentials: "include",
      signal: controller.signal,
    });
    const payload = await responsePayload(response);
    if (!response.ok) {
      const error = errorFromResponse(response, payload);
      if (response.status === 401 && !AUTH_ENTRY_PATHS.has(path)) {
        unauthorizedHandler?.(error);
      }
      throw error;
    }
    if (!isApiEnvelope(payload)) {
      throw new ApiError("后端响应不符合 API Envelope 契约", response.status, undefined, payload);
    }
    if (payload.code !== 0) {
      throw new ApiError(payload.message, response.status, payload.code, payload.data);
    }
    return payload.data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("本地后端请求超时", 408);
    }
    if (error instanceof TypeError) {
      throw new ApiError("无法连接本地后端", 0, undefined, error.message);
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function downloadFilename(response: Response): string | undefined {
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) return decodeURIComponent(utf8Match[1]);
  const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
  return plainMatch?.[1];
}

async function download(
  path: string,
  init: RequestInit = {},
): Promise<{ blob: Blob; filename?: string; headers: Headers }> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 60_000);
  const headers = requestHeaders(init);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers,
      credentials: "include",
      signal: controller.signal,
    });
    if (!response.ok) {
      const payload = await responsePayload(response);
      const error = errorFromResponse(response, payload);
      if (response.status === 401 && !AUTH_ENTRY_PATHS.has(path)) {
        unauthorizedHandler?.(error);
      }
      throw error;
    }
    return {
      blob: await response.blob(),
      filename: downloadFilename(response),
      headers: response.headers,
    };
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("本地文件传输超时", 408);
    }
    if (error instanceof TypeError) {
      throw new ApiError("无法连接本地后端", 0, undefined, error.message);
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function withJsonBody(method: string, body?: unknown): RequestInit {
  return body === undefined ? { method } : { method, body: JSON.stringify(body) };
}

export const apiClient = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, withJsonBody("POST", body)),
  put: <T>(path: string, body: unknown) => request<T>(path, withJsonBody("PUT", body)),
  patch: <T>(path: string, body: unknown) => request<T>(path, withJsonBody("PATCH", body)),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  upload: <T>(path: string, body: FormData, method = "POST") =>
    request<T>(path, { method, body }, 60_000),
  download,
};
