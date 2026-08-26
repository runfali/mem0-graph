"use client";

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { ChevronDown, Trash2, X } from "lucide-react";
import { format } from "date-fns";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { DataTable, type SortState } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { MEMORY_ENDPOINTS } from "@/utils/api-endpoints";
import { useApiQuery } from "@/hooks/use-api-query";
import { Memory, MemoryHistoryEntry } from "@/types/api";

const PAGE_SIZE = 20;
// Keep in sync with ALL_MEMORIES_LIMIT in server/main.py.
const MEMORY_FETCH_LIMIT = 1000;
const SEARCH_LIMIT = 50;

const TYPE_FILTERS = [
  { value: "all", label: "全部" },
  { value: "user", label: "用户记忆" },
  { value: "agent", label: "代理记忆" },
  { value: "generic", label: "通用" },
] as const;

const MEMORY_TYPE_FILTERS = [
  { value: "all", label: "全部类型" },
  { value: "FACTS", label: "客观事实" },
  { value: "PREFERENCES", label: "偏好" },
  { value: "EXPERIENCES", label: "经历" },
  { value: "OBSERVATIONS", label: "观察" },
  { value: "DECISIONS", label: "决策" },
] as const;

const MEMORY_TYPE_LABELS: Record<string, string> = {
  FACTS: "客观事实",
  PREFERENCES: "偏好",
  EXPERIENCES: "经历",
  OBSERVATIONS: "观察",
  DECISIONS: "决策",
};

const MEMORY_TYPE_VARIANTS: Record<
  string,
  "violet" | "pink" | "lime" | "success" | "warning"
> = {
  FACTS: "violet",
  PREFERENCES: "pink",
  EXPERIENCES: "lime",
  OBSERVATIONS: "success",
  DECISIONS: "warning",
};

const TIME_FILTERS = [
  { value: "all", label: "全部时间" },
  { value: "7", label: "近 7 天" },
  { value: "30", label: "近 30 天" },
  { value: "90", label: "近 90 天" },
] as const;

const EVENT_LABELS: Record<string, string> = {
  ADD: "新增",
  UPDATE: "更新",
  DELETE: "删除",
};

const EVENT_VARIANTS: Record<string, "success" | "violet" | "danger"> = {
  ADD: "success",
  UPDATE: "violet",
  DELETE: "danger",
};

export default function MemoriesPage() {
  return (
    <Suspense fallback={null}>
      <MemoriesContent />
    </Suspense>
  );
}

function MemoriesContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const q = searchParams.get("q");
  const qq = q?.trim() ?? "";
  const hasQuery = qq !== "";
  const searchId = searchParams.get("search");
  const [userId, setUserId] = useState("");
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [memoryTypeFilter, setMemoryTypeFilter] = useState<string>("all");
  const [timeFilter, setTimeFilter] = useState<string>("all");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);
  const [memoryToDelete, setMemoryToDelete] = useState<Memory | null>(null);
  const [history, setHistory] = useState<MemoryHistoryEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const [page, setPage] = useState(0);
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";
  const isFirstRender = useRef(true);

  const {
    data: memories = [],
    isLoading,
    refetch,
  } = useApiQuery<Memory[]>(
    async () => {
      if (hasQuery) {
        const params: Record<string, string | number> = {
          q: qq,
          limit: SEARCH_LIMIT,
        };
        if (userId.trim()) params.user_id = userId.trim();
        if (memoryTypeFilter !== "all") params.memory_type = memoryTypeFilter;
        const res = await api.get(MEMORY_ENDPOINTS.SEARCH, { params });
        const raw = res.data?.results ?? [];
        return Array.isArray(raw) ? raw : [];
      }
      const params: Record<string, string | number> = {
        top_k: MEMORY_FETCH_LIMIT,
      };
      if (userId.trim()) params.user_id = userId.trim();
      const res = await api.get(MEMORY_ENDPOINTS.BASE, { params });
      const raw = res.data?.results ?? res.data ?? [];
      return Array.isArray(raw) ? raw : [];
    },
    { errorToast: "加载记忆失败", initialData: [] },
  );

  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }
    void refetch();
  }, [qq, memoryTypeFilter, refetch]);

  useEffect(() => {
    if (!searchId) return;
    let cancelled = false;
    api
      .get(MEMORY_ENDPOINTS.BY_ID(searchId))
      .then((res) => {
        if (!cancelled) setSelectedMemory(res.data);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [searchId]);

  useEffect(() => {
    if (!selectedMemory) return;
    let cancelled = false;
    setHistory([]);
    setHistoryLoading(true);
    setHistoryError(false);
    api
      .get(MEMORY_ENDPOINTS.HISTORY(selectedMemory.id))
      .then((res) => {
        if (!cancelled) setHistory(Array.isArray(res.data) ? res.data : []);
      })
      .catch(() => {
        if (!cancelled) setHistoryError(true);
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedMemory?.id]);

  const filteredMemories = useMemo(() => {
    if (hasQuery) return memories;
    return memories.filter((m) => {
      if (typeFilter !== "all") {
        const type = m.agent_id
          ? "agent"
          : m.user_id
            ? "user"
            : "generic";
        if (type !== typeFilter) return false;
      }
      if (memoryTypeFilter !== "all") {
        const mt =
          m.memory_type ??
          (m as Memory & { metadata?: { memory_type?: string } }).metadata
            ?.memory_type;
        if (mt !== memoryTypeFilter) return false;
      }
      if (timeFilter !== "all" && m.created_at) {
        const days = Number(timeFilter);
        const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
        if (new Date(m.created_at).getTime() < cutoff) return false;
      }
      return true;
    });
  }, [memories, hasQuery, typeFilter, memoryTypeFilter, timeFilter]);

  // 列排序：对全量过滤结果生效后再分页（DataTable 为受控模式，只渲染表头态）
  const [memSort, setMemSort] = useState<SortState<Memory> | null>(null);
  const sortedMemories = useMemo(() => {
    if (!memSort) return filteredMemories;
    // 「创建时间」列展示的是 updated_at ?? created_at，排序键与其一致；
    // 其余列直接取原值比较
    const valueOf = (row: Memory): string | number =>
      memSort.key === "created_at"
        ? (row.updated_at ?? row.created_at ?? "")
        : ((row[memSort.key] ?? "") as string | number);
    const copy = [...filteredMemories];
    copy.sort((a, b) => {
      const va = valueOf(a);
      const vb = valueOf(b);
      const cmp =
        typeof va === "number" && typeof vb === "number"
          ? va - vb
          : String(va).localeCompare(String(vb), "zh-CN");
      return memSort.direction === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [filteredMemories, memSort]);

  const totalPages = Math.ceil(sortedMemories.length / PAGE_SIZE);
  const paginatedMemories = sortedMemories.slice(
    page * PAGE_SIZE,
    (page + 1) * PAGE_SIZE,
  );

  const pageSelectedCount = paginatedMemories.filter((m) =>
    selectedIds.has(m.id),
  ).length;
  const allPageSelected =
    paginatedMemories.length > 0 &&
    pageSelectedCount === paginatedMemories.length;
  const somePageSelected =
    pageSelectedCount > 0 && pageSelectedCount < paginatedMemories.length;

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      const pageIds = paginatedMemories.map((m) => m.id);
      if (allPageSelected) pageIds.forEach((id) => next.delete(id));
      else pageIds.forEach((id) => next.add(id));
      return next;
    });
  };

  const handleDelete = async () => {
    if (!memoryToDelete) return;
    try {
      await api.delete(MEMORY_ENDPOINTS.BY_ID(memoryToDelete.id));
      toast({ title: "记忆已删除", variant: "success" });
      if (selectedMemory?.id === memoryToDelete.id) setSelectedMemory(null);
      setSelectedIds((prev) => {
        const next = new Set(prev);
        next.delete(memoryToDelete.id);
        return next;
      });
      setMemoryToDelete(null);
      void refetch();
    } catch (error) {
      toast({
        title: "删除记忆失败",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    }
  };

  const handleBatchDelete = async () => {
    const ids = Array.from(selectedIds);
    try {
      for (const id of ids) {
        await api.delete(MEMORY_ENDPOINTS.BY_ID(id));
      }
      toast({ title: `已删除 ${ids.length} 条记忆`, variant: "success" });
      setSelectedIds(new Set());
      setBatchDeleteOpen(false);
      if (selectedMemory && ids.includes(selectedMemory.id))
        setSelectedMemory(null);
      void refetch();
    } catch (error) {
      setBatchDeleteOpen(false);
      void refetch();
      toast({
        title: "批量删除失败",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    }
  };

  const columns = [
    {
      key: "id" as keyof Memory,
      label: "",
      width: 40,
      headerVariant: "check" as const,
      cellVariant: "flush" as const,
      className: "px-4 py-2.5 align-middle",
      render: (value: string, row: Memory) => (
        <div className="flex items-center">
          <Checkbox
            checked={selectedIds.has(row.id)}
            onCheckedChange={() => toggleSelect(row.id)}
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      ),
    },
    {
      key: "memory" as keyof Memory,
      label: "内容",
      width: 360,
      sortable: true,
      sortValue: (row: Memory) => row.memory ?? "",
      render: (value: string) => (
        <span className="line-clamp-2 text-sm">{value}</span>
      ),
    },
    {
      key: "user_id" as keyof Memory,
      label: "用户",
      width: 90,
      sortable: true,
      sortValue: (row: Memory) => row.user_id ?? "",
    },
    {
      key: "agent_id" as keyof Memory,
      label: "代理",
      width: 90,
      sortable: true,
      sortValue: (row: Memory) => row.agent_id ?? "",
    },
    {
      key: "created_at" as keyof Memory,
      label: "创建时间",
      width: 105,
      sortable: true,
      // 排序键用完整 ISO 时间戳（展示为日期），保证同日多条可精确排序
      sortValue: (row: Memory) => row.created_at ?? "",
      render: (value: string) =>
        value ? format(new Date(value), "MMM d, yyyy") : "--",
    },
    {
      key: "updated_at" as keyof Memory,
      label: "修改时间",
      width: 105,
      sortable: true,
      sortValue: (row: Memory) => row.updated_at ?? row.created_at ?? "",
      render: (value: string, row: Memory) => {
        const t = value || row.created_at;
        return t ? format(new Date(t), "MMM d, yyyy") : "--";
      },
    },
  ];

  const typeBadge = (mt?: string) => {
    if (!mt || !MEMORY_TYPE_LABELS[mt]) return null;
    return (
      <Badge variant={MEMORY_TYPE_VARIANTS[mt]} className="shrink-0">
        {MEMORY_TYPE_LABELS[mt]}
      </Badge>
    );
  };

  const searchColumns = [
    {
      key: "memory" as keyof Memory,
      label: "内容",
      width: 400,
      sortable: true,
      sortValue: (row: Memory) => row.memory ?? "",
      render: (value: string, row: Memory) => (
        <div className="flex items-start gap-2">
          <span className="line-clamp-2 flex-1 text-sm">{value}</span>
          {typeBadge(row.memory_type)}
        </div>
      ),
    },
    {
      key: "user_id" as keyof Memory,
      label: "用户",
      width: 100,
      sortable: true,
      sortValue: (row: Memory) => row.user_id ?? "",
    },
    {
      key: "agent_id" as keyof Memory,
      label: "代理",
      width: 100,
      sortable: true,
      sortValue: (row: Memory) => row.agent_id ?? "",
    },
    {
      key: "created_at" as keyof Memory,
      label: "更新时间",
      width: 120,
      sortable: true,
      sortValue: (row: Memory) => row.updated_at ?? row.created_at ?? "",
      render: (value: string, row: Memory) => {
        const t = row.updated_at ?? value;
        return t ? format(new Date(t), "MMM d, yyyy") : "--";
      },
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold font-fustat">
          {hasQuery ? `搜索结果：${qq}` : "记忆"}
        </h1>
        {hasQuery && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => router.replace("/dashboard/memories")}
          >
            <X className="size-3.5 mr-1" />
            清除搜索
          </Button>
        )}
      </div>

      {hasQuery ? (
        isLoading ? (
          <TableSkeleton rows={5} columns={4} />
        ) : memories.length === 0 ? (
          <EmptyState
            title="没有匹配的记忆"
            description={`没有找到与“${qq}”相关的记忆。`}
          />
        ) : (
          <>
            <Card className="border-memBorder-primary overflow-hidden">
              <DataTable
                data={memories}
                columns={searchColumns}
                getRowKey={(row) => row.id}
                onRowClick={(row) => setSelectedMemory(row)}
                getRowClassName={(row) =>
                  selectedMemory?.id === row.id
                    ? "bg-surface-default-tertiary"
                    : undefined
                }
              />
            </Card>
          </>
        )
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <Input
              placeholder="按用户 ID 筛选（可选）"
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  setPage(0);
                  refetch();
                }
              }}
              className="w-64"
            />
            <Select
              value={typeFilter}
              onValueChange={(v) => {
                setTypeFilter(v);
                setPage(0);
              }}
            >
              <SelectTrigger variant="dropdown" className="w-40">
                <SelectValue placeholder="全部" />
              </SelectTrigger>
              <SelectContent>
                {TYPE_FILTERS.map((f) => (
                  <SelectItem key={f.value} value={f.value}>
                    {f.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={memoryTypeFilter}
              onValueChange={(v) => {
                setMemoryTypeFilter(v);
                setPage(0);
              }}
            >
              <SelectTrigger variant="dropdown" className="w-40">
                <SelectValue placeholder="全部类型" />
              </SelectTrigger>
              <SelectContent>
                {MEMORY_TYPE_FILTERS.map((f) => (
                  <SelectItem key={f.value} value={f.value}>
                    {f.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={timeFilter}
              onValueChange={(v) => {
                setTimeFilter(v);
                setPage(0);
              }}
            >
              <SelectTrigger variant="dropdown" className="w-40">
                <SelectValue placeholder="全部时间" />
              </SelectTrigger>
              <SelectContent>
                {TIME_FILTERS.map((f) => (
                  <SelectItem key={f.value} value={f.value}>
                    {f.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {selectedIds.size > 0 && (
            <div className="flex items-center justify-between rounded-lg border border-memBorder-primary bg-surface-default-tertiary px-4 py-2.5">
              <span className="text-sm text-onSurface-default-primary">
                已选 {selectedIds.size} 条
              </span>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setSelectedIds(new Set())}
                >
                  清空选择
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => setBatchDeleteOpen(true)}
                >
                  <Trash2 className="size-3.5 mr-1" />
                  批量删除
                </Button>
              </div>
            </div>
          )}

          {isLoading ? (
            <TableSkeleton rows={5} columns={5} />
          ) : filteredMemories.length === 0 ? (
            <EmptyState
              title="还没有记忆"
              description="发送 POST /memories 请求，创建你的第一条记忆。"
            >
              <pre className="text-xs text-left bg-surface-default-secondary p-3 rounded font-mono overflow-x-auto mt-3 max-w-lg">
                {`curl -X POST ${apiUrl}/memories \\
  -H "X-API-Key: <your-key>" \\
  -H "Content-Type: application/json" \\
  -d '{"messages": [{"role": "user", "content": "I like hiking"}], "user_id": "alice"}'`}
              </pre>
              <a
                href="https://docs.mem0.ai/open-source/features/rest-api#memory-operations"
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-onSurface-default-tertiary underline underline-offset-4 hover:text-onSurface-default-primary mt-2"
              >
                REST API 参考文档
              </a>
            </EmptyState>
          ) : (
            <>
              <Card className="border-memBorder-primary overflow-hidden">
                <DataTable
                  data={paginatedMemories}
                  columns={columns}
                  sort={memSort}
                  onSortChange={setMemSort}
                  getRowKey={(row) => row.id}
                  onRowClick={(row) => setSelectedMemory(row)}
                  getRowClassName={(row) =>
                    selectedMemory?.id === row.id
                      ? "bg-surface-default-tertiary"
                      : undefined
                  }
                  selectAll={{
                    checked: allPageSelected,
                    indeterminate: somePageSelected,
                    onSelectAll: toggleSelectAll,
                  }}
                />
              </Card>
              {totalPages > 1 && (
                <div className="flex items-center justify-between text-sm text-onSurface-default-tertiary">
                  <span>
                    第 {page * PAGE_SIZE + 1}–
                    {Math.min((page + 1) * PAGE_SIZE, sortedMemories.length)}{" "}
                    条，共 {sortedMemories.length} 条
                  </span>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={page === 0}
                      onClick={() => setPage((p) => p - 1)}
                    >
                      上一页
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={page >= totalPages - 1}
                      onClick={() => setPage((p) => p + 1)}
                    >
                      下一页
                    </Button>
                  </div>
                </div>
              )}
            </>
          )}
        </>
      )}

      <Sheet
        open={!!selectedMemory}
        onOpenChange={(open) => {
          if (!open) setSelectedMemory(null);
        }}
      >
        <SheetContent className="sm:max-w-md overflow-y-auto">
          <SheetHeader>
            <SheetTitle>记忆详情</SheetTitle>
            <SheetDescription className="sr-only">
              查看记忆内容与元数据
            </SheetDescription>
          </SheetHeader>
          {selectedMemory && (
            <div className="mt-6 space-y-4">
              <div className="space-y-1">
                <Label className="text-xs text-onSurface-default-tertiary">
                  内容
                </Label>
                <p className="text-sm whitespace-pre-wrap break-words">
                  {selectedMemory.memory}
                </p>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1">
                  <Label className="text-xs text-onSurface-default-tertiary">
                    ID
                  </Label>
                  <p className="text-xs font-mono break-all">
                    {selectedMemory.id}
                  </p>
                </div>
                {selectedMemory.user_id && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      用户
                    </Label>
                    <p className="text-sm">{selectedMemory.user_id}</p>
                  </div>
                )}
                {selectedMemory.agent_id && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      代理
                    </Label>
                    <p className="text-sm">{selectedMemory.agent_id}</p>
                  </div>
                )}
                {selectedMemory.memory_type && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      类型
                    </Label>
                    <div>{typeBadge(selectedMemory.memory_type)}</div>
                  </div>
                )}
                {selectedMemory.created_at && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      创建时间
                    </Label>
                    <p className="text-sm">
                      {new Date(selectedMemory.created_at).toLocaleString()}
                    </p>
                  </div>
                )}
              </div>
              <Button
                variant="outline"
                size="sm"
                className="text-onSurface-danger-primary"
                onClick={() => setMemoryToDelete(selectedMemory)}
              >
                <Trash2 className="size-3.5 mr-1" />
                删除记忆
              </Button>

              <div className="border-t border-memBorder-primary pt-4">
                <Collapsible defaultOpen>
                  <CollapsibleTrigger asChild>
                    <button
                      type="button"
                      className="flex w-full items-center justify-between text-sm font-medium text-onSurface-default-primary"
                    >
                      <span>历史记录</span>
                      <ChevronDown className="size-4 text-onSurface-default-tertiary" />
                    </button>
                  </CollapsibleTrigger>
                  <CollapsibleContent className="mt-3 space-y-3">
                    {historyLoading ? (
                      <p className="text-sm text-onSurface-default-tertiary">
                        加载中…
                      </p>
                    ) : historyError ? (
                      <p className="text-sm text-onSurface-default-tertiary">
                        历史加载失败
                      </p>
                    ) : history.length === 0 ? (
                      <p className="text-sm text-onSurface-default-tertiary">
                        暂无历史记录
                      </p>
                    ) : (
                      history.map((h) => {
                        const label = EVENT_LABELS[h.event] ?? "其他";
                        const variant = EVENT_VARIANTS[h.event];
                        const isCurrent =
                          h.event === "UPDATE" &&
                          !!h.new_memory &&
                          h.new_memory === selectedMemory.memory;
                        const changed =
                          !!h.old_memory &&
                          !!h.new_memory &&
                          h.old_memory !== h.new_memory;
                        return (
                          <div
                            key={h.id}
                            className="space-y-1.5 rounded-md border border-memBorder-primary bg-surface-default-primary p-3"
                          >
                            <div className="flex items-center justify-between">
                              <Badge variant={variant}>{label}</Badge>
                              <span className="text-xs text-onSurface-default-tertiary">
                                {h.created_at
                                  ? format(
                                      new Date(h.created_at),
                                      "MMM d, yyyy HH:mm",
                                    )
                                  : "--"}
                              </span>
                            </div>
                            {changed ? (
                              <div className="space-y-1 text-xs">
                                <p className="line-clamp-2 whitespace-pre-wrap break-words text-onSurface-default-tertiary">
                                  {h.old_memory}
                                </p>
                                <p className="text-onSurface-default-tertiary">
                                  ↓
                                </p>
                                <div className="flex items-start gap-2">
                                  <p className="line-clamp-3 flex-1 whitespace-pre-wrap break-words text-onSurface-default-primary">
                                    {h.new_memory}
                                  </p>
                                  {isCurrent && (
                                    <Badge variant="lime" className="shrink-0">
                                      当前内容
                                    </Badge>
                                  )}
                                </div>
                              </div>
                            ) : h.new_memory ? (
                              <div className="flex items-start gap-2">
                                <p className="line-clamp-3 flex-1 whitespace-pre-wrap break-words text-xs text-onSurface-default-primary">
                                  {h.new_memory}
                                </p>
                                {isCurrent && (
                                  <Badge variant="lime" className="shrink-0">
                                    当前内容
                                  </Badge>
                                )}
                              </div>
                            ) : h.old_memory ? (
                              <p className="line-clamp-3 whitespace-pre-wrap break-words text-xs text-onSurface-default-tertiary">
                                {h.old_memory}
                              </p>
                            ) : null}
                          </div>
                        );
                      })
                    )}
                  </CollapsibleContent>
                </Collapsible>
              </div>
            </div>
          )}
        </SheetContent>
      </Sheet>

      <DeleteConfirmationModal
        isOpen={!!memoryToDelete}
        onClose={() => setMemoryToDelete(null)}
        onConfirm={handleDelete}
        title="删除记忆"
        description="该记忆将被永久删除，此操作无法撤销。"
        itemName={memoryToDelete?.id ?? ""}
        confirmButtonText="删除"
      />

      <AlertDialog open={batchDeleteOpen} onOpenChange={setBatchDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>批量删除</AlertDialogTitle>
            <AlertDialogDescription>
              确定删除选中的 {selectedIds.size} 条记忆？此操作不可恢复。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={(e) => {
                e.preventDefault();
                void handleBatchDelete();
              }}
            >
              删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
