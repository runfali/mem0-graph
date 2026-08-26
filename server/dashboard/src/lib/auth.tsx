"use client";

import {
  createContext,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api, refreshAccessToken, setAccessToken } from "@/utils/api";
import { AUTH_ENDPOINTS } from "@/utils/api-endpoints";

export interface AuthUser {
  id: string;
  name: string;
  email: string;
  role: string;
  created_at: string;
}

interface AuthContextValue {
  user: AuthUser | null;
  isLoading: boolean;
  isAdmin: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (name: string, email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue>({
  user: null,
  isLoading: true,
  isAdmin: false,
  login: async () => {},
  register: async () => {},
  logout: async () => {},
  refreshUser: async () => {},
});

async function storeRefreshToken(refreshToken: string) {
  await fetch("/api/auth/refresh", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
}

async function clearRefreshToken() {
  await fetch("/api/auth/refresh", { method: "DELETE" });
}

async function refreshSession(): Promise<boolean> {
  // 二轮审计 fix-3：并入与 axios 拦截器相同的 single-flight 刷新通道——
  // 冷启动时 AuthProvider 与页面请求若各自 POST /auth/refresh，后端 jti
  // 单次有效，落败方会触发登出；共享同一 promise 后只有一路真正刷新。
  const token = await refreshAccessToken();
  return token !== null;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const loadUser = useCallback(async () => {
    const res = await api.get<AuthUser>(AUTH_ENDPOINTS.ME);
    setUser(res.data);
  }, []);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const ok = await refreshSession();
        if (ok && active) await loadUser();
      } catch {
        if (active) setUser(null);
      } finally {
        if (active) setIsLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [loadUser]);

  const login = useCallback(
    async (email: string, password: string) => {
      const res = await api.post(AUTH_ENDPOINTS.LOGIN, { email, password });
      setAccessToken(res.data.access_token);
      await storeRefreshToken(res.data.refresh_token);
      await loadUser();
    },
    [loadUser],
  );

  const register = useCallback(
    async (name: string, email: string, password: string) => {
      const res = await api.post(AUTH_ENDPOINTS.REGISTER, {
        name,
        email,
        password,
      });
      setAccessToken(res.data.access_token);
      await storeRefreshToken(res.data.refresh_token);
      await loadUser();
    },
    [loadUser],
  );

  const logout = useCallback(async () => {
    await clearRefreshToken();
    setAccessToken(null);
    setUser(null);
    if (typeof window !== "undefined") window.location.href = "/login";
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isLoading,
      isAdmin: user?.role === "admin",
      login,
      register,
      logout,
      refreshUser: loadUser,
    }),
    [user, isLoading, login, register, logout, loadUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
