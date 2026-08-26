"use client";

import { formatDistanceToNow } from "date-fns";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, ChartLine, FolderCog, GalleryVerticalEnd } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { DataTable } from "@/components/shared/data-table";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/utils/api";
import {
  MEMORY_ENDPOINTS,
  REQUEST_ENDPOINTS,
  ENTITY_ENDPOINTS,
} from "@/utils/api-endpoints";
import { useApiQuery } from "@/hooks/use-api-query";
import { ApiRequestLog, ApiRequestLogList, Entity, Memory } from "@/types/api";

// Keep in sync with ALL_MEMORIES_LIMIT in server/main.py.
const MEMORY_FETCH_LIMIT = 1000;
const REQUEST_LOG_LIMIT = 200;
const RECENT_LIMIT = 10;

type RequestLog = {
  id: string;
  createdAt: string;
  method: string;
  path: string;
  statusCode: number;
  latencyMs: number;
};

type DashboardData = {
  memories: Memory[];
  entities: Entity[];
  requests: RequestLog[];
  totalRequests: number;
};

const getStatusBadge = (
  statusCode: number,
): "danger" | "warning" | "success" => {
  if (statusCode >= 500) return "danger";
  if (statusCode >= 400) return "warning";
  return "success";
};

const getMethodBadge = (
  method: string,
): "lime" | "violet" | "pink" | "outline" => {
  switch (method.toUpperCase()) {
    case "POST":
      return "lime";
    case "PUT":
    case "PATCH":
      return "violet";
    case "DELETE":
      return "pink";
    default:
      return "outline";
  }
};

const getMemoryType = (memory: Memory): { label: string; variant: "lime" | "violet" | "outline" } => {
  if (memory.agent_id) return { label: "代理", variant: "violet" };
  if (memory.user_id) return { label: "用户", variant: "lime" };
  return { label: "通用", variant: "outline" };
};

const normalizeLog = (entry: ApiRequestLog): RequestLog => ({
  id: entry.id,
  createdAt: entry.created_at,
  method: entry.method,
  path: entry.path,
  statusCode: entry.status_code,
  latencyMs: entry.latency_ms,
});

export default function DashboardPage() {
  const router = useRouter();
  const { data, isLoading, error } = useApiQuery<DashboardData>(
    async () => {
      const [memoriesRes, entitiesRes, requestsRes] = await Promise.all([
        api.get<{ results: Memory[] }>(MEMORY_ENDPOINTS.BASE, {
          params: { top_k: MEMORY_FETCH_LIMIT },
        }),
        api.get<Entity[]>(ENTITY_ENDPOINTS.BASE),
        api.get<ApiRequestLogList>(REQUEST_ENDPOINTS.BASE, {
          params: { limit: REQUEST_LOG_LIMIT },
        }),
      ]);
      const rawMemories = memoriesRes.data?.results ?? memoriesRes.data ?? [];
      return {
        memories: Array.isArray(rawMemories) ? rawMemories : [],
        entities: entitiesRes.data ?? [],
        requests: (requestsRes.data?.items ?? []).map(normalizeLog),
        totalRequests: requestsRes.data?.total ?? 0,
      };
    },
    {
      errorToast: "加载仪表盘数据失败",
      initialData: { memories: [], entities: [], requests: [], totalRequests: 0 },
    },
  );

  const { memories = [], entities = [], requests = [], totalRequests = requests.length } = data ?? {};

  // 「最近记忆」按更新时间倒序显式排序后再取前 N：接口返回顺序是存储层物理序
  // （vector_store.list 无 ORDER BY），不排序会显示成任意一批旧记忆。
  const recentMemories = [...memories]
    .sort((a, b) =>
      (b.updated_at ?? b.created_at ?? "").localeCompare(a.updated_at ?? a.created_at ?? ""),
    )
    .slice(0, RECENT_LIMIT);

  const windowCount = requests.length;
  const successfulRequests = requests.filter((log) => log.statusCode < 400).length;
  const successRate =
    windowCount > 0
      ? Math.round((successfulRequests / windowCount) * 100)
      : 0;
  const averageLatency =
    windowCount > 0
      ? Math.round(
          requests.reduce((sum, log) => sum + log.latencyMs, 0) / windowCount,
        )
      : 0;

  const stats = [
    { label: "记忆总数", value: memories.length },
    { label: "实体数", value: entities.length },
    { label: "请求总数", value: totalRequests },
    {
      label: "成功率",
      value: totalRequests > 0 ? `${successRate}%` : "--",
    },
    {
      label: "平均延迟",
      value: totalRequests > 0 ? `${averageLatency} ms` : "--",
    },
  ];

  const requestColumns = [
    {
      key: "createdAt" as keyof RequestLog,
      label: "时间",
      width: 110,
      render: (value: string) => (
        <span className="text-xs whitespace-nowrap">
          {formatDistanceToNow(new Date(value), { addSuffix: true })}
        </span>
      ),
    },
    {
      key: "method" as keyof RequestLog,
      label: "方法",
      width: 80,
      render: (value: string) => (
        <Badge variant={getMethodBadge(value)}>{value.toUpperCase()}</Badge>
      ),
    },
    {
      key: "path" as keyof RequestLog,
      label: "路径",
      width: 300,
      render: (value: string) => (
        <span className="font-mono text-xs break-all text-onSurface-default-primary">
          {value}
        </span>
      ),
    },
    {
      key: "statusCode" as keyof RequestLog,
      label: "状态",
      width: 80,
      render: (value: number) => (
        <Badge variant={getStatusBadge(value)}>{value}</Badge>
      ),
    },
    {
      key: "latencyMs" as keyof RequestLog,
      label: "延迟",
      width: 80,
      render: (value: number) => (
        <span className="text-xs tabular-nums">{value} ms</span>
      ),
    },
  ];

  const memoryColumns = [
    {
      key: "memory" as keyof Memory,
      label: "内容",
      width: 320,
      render: (value: string) => (
        <span className="line-clamp-2 text-xs text-onSurface-default-primary">
          {value}
        </span>
      ),
    },
    {
      key: "id" as keyof Memory,
      label: "类型",
      width: 90,
      render: (_: string, row: Memory) => {
        const { label, variant } = getMemoryType(row);
        return <Badge variant={variant}>{label}</Badge>;
      },
    },
    {
      key: "created_at" as keyof Memory,
      label: "时间",
      width: 110,
      render: (value: string | undefined) => (
        <span className="text-xs whitespace-nowrap">
          {value
            ? formatDistanceToNow(new Date(value), { addSuffix: true })
            : "--"}
        </span>
      ),
    },
  ];

  const quickLinks = [
    { label: "查看分析", url: "/dashboard/analytics", icon: ChartLine },
    { label: "管理记忆", url: "/dashboard/memories", icon: GalleryVerticalEnd },
    { label: "系统配置", url: "/dashboard/configuration", icon: FolderCog },
  ];

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold font-fustat">仪表盘</h1>
        <p className="text-sm text-onSurface-default-secondary">
          你的自托管实例概览。
        </p>
      </div>

      {isLoading ? (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-5">
          {[...Array(5)].map((_, i) => (
            <Card key={i} className="border-sentry-hairline">
              <CardContent className="space-y-3 p-4">
                <Skeleton className="h-3 w-16" />
                <Skeleton className="h-7 w-20" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-5">
          {stats.map((card) => (
            <Card
              key={card.label}
              className="relative border-sentry-hairline overflow-hidden"
            >
              <div className="absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r from-sentry-lime via-sentry-violet to-sentry-pink" />
              <CardContent className="p-4 pt-5">
                <p className="text-[11px] font-semibold uppercase tracking-[0.06em] text-onSurface-default-tertiary">
                  {card.label}
                </p>
                <p className="mt-1.5 text-[26px] font-bold tabular-nums leading-none text-onSurface-default-primary">
                  {card.value}
                </p>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {error && (
        <Card className="border-memBorder-primary">
          <CardContent className="p-4 text-sm text-onSurface-danger-primary">
            {error}
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card className="border-memBorder-primary overflow-hidden">
          <CardContent className="p-4">
            <div className="flex items-center justify-between mb-3">
              <p className="text-sm font-semibold text-onSurface-default-primary">
                最近请求
              </p>
              <Link
                href="/dashboard/requests"
                className="text-xs text-onSurface-default-tertiary hover:text-onSurface-default-primary flex items-center gap-1"
              >
                查看全部 <ArrowRight className="size-3" />
              </Link>
            </div>
            {isLoading ? (
              <div className="space-y-2">
                {[...Array(5)].map((_, i) => (
                  <Skeleton key={i} className="h-8 w-full" />
                ))}
              </div>
            ) : requests.length === 0 ? (
              <p className="py-10 text-center text-sm text-onSurface-default-tertiary">
                暂无数据
              </p>
            ) : (
              <DataTable
                data={requests.slice(0, RECENT_LIMIT)}
                columns={requestColumns}
                getRowKey={(row) => row.id}
                onRowClick={() => {
                  router.push("/dashboard/requests");
                }}
              />
            )}
          </CardContent>
        </Card>

        <Card className="border-memBorder-primary overflow-hidden">
          <CardContent className="p-4">
            <div className="flex items-center justify-between mb-3">
              <p className="text-sm font-semibold text-onSurface-default-primary">
                最近记忆
              </p>
              <Link
                href="/dashboard/memories"
                className="text-xs text-onSurface-default-tertiary hover:text-onSurface-default-primary flex items-center gap-1"
              >
                查看全部 <ArrowRight className="size-3" />
              </Link>
            </div>
            {isLoading ? (
              <div className="space-y-2">
                {[...Array(5)].map((_, i) => (
                  <Skeleton key={i} className="h-8 w-full" />
                ))}
              </div>
            ) : memories.length === 0 ? (
              <p className="py-10 text-center text-sm text-onSurface-default-tertiary">
                暂无数据
              </p>
            ) : (
              <DataTable
                data={recentMemories}
                columns={memoryColumns}
                getRowKey={(row) => row.id}
                onRowClick={() => {
                  router.push("/dashboard/memories");
                }}
              />
            )}
          </CardContent>
        </Card>
      </div>

      <div className="flex flex-wrap gap-3">
        {quickLinks.map((link) => (
          <Button asChild key={link.url} variant="outline">
            <Link href={link.url} className="gap-2">
              <link.icon className="size-4" />
              {link.label}
            </Link>
          </Button>
        ))}
      </div>
    </div>
  );
}
