import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";
import { AUTH_ENDPOINTS } from "@/utils/api-endpoints";
import { getServerApiUrl } from "@/lib/server-api-url";

const COOKIE_NAME = "mem0_refresh_token";

function shouldUseSecureCookie() {
  const dashboardUrl = process.env.DASHBOARD_URL;
  if (!dashboardUrl) {
    return process.env.NODE_ENV === "production";
  }

  try {
    return new URL(dashboardUrl).protocol === "https:";
  } catch {
    return process.env.NODE_ENV === "production";
  }
}

const COOKIE_OPTIONS = {
  httpOnly: true,
  secure: shouldUseSecureCookie(),
  sameSite: "lax" as const,
  path: "/",
  maxAge: 30 * 24 * 60 * 60, // 30 days
};

export async function POST() {
  const cookieStore = await cookies();
  const refreshToken = cookieStore.get(COOKIE_NAME)?.value;

  if (!refreshToken) {
    return NextResponse.json({ error: "No refresh token" }, { status: 401 });
  }

  const res = await fetch(`${getServerApiUrl()}${AUTH_ENDPOINTS.REFRESH}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });

  if (!res.ok) {
    // 三轮审计：失败分支不再主动删除 cookie——Next Route Handler 的 cookies()
    // 读取的是请求进入时的快照，与本次消费的 token 恒同源，比较恒真；多标签页
    // 并发刷新时落败方会抹掉另一标签页刚轮换成功的新 cookie。失效 token 残留
    // 无害（下次 POST 自然 401 走登录流程），登出清理交给 DELETE 端点。
    return NextResponse.json({ error: "Refresh failed" }, { status: 401 });
  }

  const data = await res.json();

  cookieStore.set(COOKIE_NAME, data.refresh_token, COOKIE_OPTIONS);

  return NextResponse.json({ access_token: data.access_token });
}

export async function PUT(request: NextRequest) {
  const body = await request.json();
  const cookieStore = await cookies();

  if (!body.refresh_token) {
    return NextResponse.json(
      { error: "Missing refresh_token" },
      { status: 400 },
    );
  }

  // 种 cookie 前先向后端校验真伪：无校验的全局写入点是会话固定/污染面
  // （同源脚本可把受害者 cookie 换成攻击者 token 或垃圾值）。后端为单次
  // 轮换制：校验即消费旧 jti，必须以轮换后的新 refresh_token 落 cookie。
  const verify = await fetch(`${getServerApiUrl()}${AUTH_ENDPOINTS.REFRESH}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: body.refresh_token }),
  });

  if (!verify.ok) {
    return NextResponse.json({ error: "Invalid refresh token" }, { status: 401 });
  }

  const data = await verify.json();
  cookieStore.set(COOKIE_NAME, data.refresh_token, COOKIE_OPTIONS);
  return NextResponse.json({ ok: true });
}

export async function DELETE() {
  const cookieStore = await cookies();
  const refreshToken = cookieStore.get(COOKIE_NAME)?.value;
  if (refreshToken) {
    // 四轮审计：登出时吊销服务端 jti——此前仅删 cookie，被泄露的 refresh
    // token 30 天内仍可换新 access；吊销幂等（未知/已吊销都返回 ok）
    try {
      // 五轮审计：吊销失败必须可观测（此前静默降级——后端 429/500 时 jti
      // 残留 30 天可重放窗口无人知晓）；失败不阻塞登出（cookie 仍清除）
      const revokeRes = await fetch(`${getServerApiUrl()}${AUTH_ENDPOINTS.REVOKE_REFRESH}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
        signal: AbortSignal.timeout(5000),
      });
      if (!revokeRes.ok) {
        console.error(`[auth] revoke-refresh failed: HTTP ${revokeRes.status}`);
      }
    } catch (error) {
      console.error("[auth] revoke-refresh error:", error);
    }
  }
  cookieStore.delete(COOKIE_NAME);
  return NextResponse.json({ ok: true });
}
