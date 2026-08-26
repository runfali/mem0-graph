import axios, { AxiosError, AxiosInstance } from "axios";

let cachedToken: string | null = null;
const LOGIN_PATH = "/login";

export const setAccessToken = (token: string | null) => {
  cachedToken = token;
};

export const getAccessToken = (): string | null => {
  return cachedToken;
};

const handleTokenError = () => {
  cachedToken = null;
};

const redirectToLogin = () => {
  if (typeof window !== "undefined") {
    window.location.href = LOGIN_PATH;
  }
};

// single-flight 刷新：并发 401 共享同一次 POST /api/auth/refresh。
// 后端 refresh jti 单次有效（轮换制）：若无去重，多请求页面的并发 401
// （冷启动、access token 过期后的聚合查询）必然产生多次刷新竞争，失败的
// 并发方会把已正常续期的用户硬踢回登录页（spurious logout）。
let refreshPromise: Promise<string | null> | null = null;

export const refreshAccessToken = (): Promise<string | null> => {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const refreshResponse = await fetch("/api/auth/refresh", {
          method: "POST",
          credentials: "include",
        });
        if (!refreshResponse.ok) return null;
        const data = await refreshResponse.json();
        setAccessToken(data.access_token);
        return data.access_token as string;
      } catch {
        // 网络错误/响应解析失败一律视为刷新失败，保证 promise 永不 reject
        return null;
      } finally {
        refreshPromise = null;
      }
    })();
  }
  return refreshPromise;
};

type RetriableConfig = NonNullable<AxiosError["config"]> & { _retry?: boolean };

const createApi = (): AxiosInstance & {
  postStream: (url: string, data: unknown) => Promise<Response>;
} => {
  const api = axios.create({
    baseURL: process.env.NEXT_PUBLIC_API_URL,
  });

  api.interceptors.request.use(
    async (config) => {
      if (cachedToken) {
        config.headers = config.headers ?? {};
        config.headers.Authorization = `Bearer ${cachedToken}`;
      }
      return config;
    },
    (error) => {
      return Promise.reject(error);
    },
  );

  api.interceptors.response.use(
    (response) => response,
    async (error: AxiosError<{ error?: string }>) => {
      const config = error.config as RetriableConfig | undefined;
      if (error.response?.status === 401 && config && !config._retry) {
        // 先等 single-flight 刷新结果：成功则换新 token 重放一次；
        // 刷新失败才清会话并登出。_retry 防止重放仍 401 时的二次刷新循环。
        const nextToken = await refreshAccessToken();
        if (nextToken) {
          config._retry = true;
          config.headers = config.headers ?? {};
          config.headers.Authorization = `Bearer ${nextToken}`;
          return api.request(config);
        }

        handleTokenError();
        redirectToLogin();
      }

      if (error.response?.data?.error) {
        return Promise.reject(error.response.data.error);
      }

      return Promise.reject(error);
    },
  );

  const postStream = async (url: string, data: unknown): Promise<Response> => {
    const doFetch = (token: string | null) =>
      fetch(`${process.env.NEXT_PUBLIC_API_URL}${url}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: token ? `Bearer ${token}` : "",
        },
        body: JSON.stringify(data),
      });

    let response = await doFetch(cachedToken);

    if (response.status === 401) {
      // 与 axios 拦截器共用同一 single-flight 刷新：刷新成功重发一次，
      // 仍 401 才清会话登出（与其他并发请求的刷新结果保持一致）
      const nextToken = await refreshAccessToken();
      if (nextToken) {
        response = await doFetch(nextToken);
      }
      if (response.status === 401) {
        handleTokenError();
        redirectToLogin();
        throw new Error("未授权");
      }
    }

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.error || "请求失败");
    }

    return response;
  };

  return Object.assign(api, { postStream });
};

export const api = createApi();
