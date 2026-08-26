"use client";

import { ReactNode, useMemo, useState } from "react";
import { LucideIcon } from "lucide-react";
import { ChevronDown, ChevronUp, ChevronsUpDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";

interface Column<T> {
  key: keyof T;
  label: string;
  icon?: LucideIcon;
  render?(value: T[keyof T], row: T): ReactNode;
  className?: string;
  width?: number | "auto";
  align?: "left" | "center" | "right";
  cellVariant?: "default" | "flush";
  headerVariant?: "default" | "check";
  /**
   * 表头可点击排序（升 → 降循环）。排序键取 sortValue(row)，未提供时回退
   * row[key] 原值——渲染值是格式化文本（如相对时间）的列应提供 sortValue。
   */
  sortable?: boolean;
  sortValue?(row: T): string | number;
}

type SortDirection = "asc" | "desc";
export interface SortState<T> {
  key: keyof T;
  direction: SortDirection;
}

interface DataTableProps<T> {
  data: T[];
  columns: Column<T>[];
  className?: string;
  getRowKey?: (row: T, rowIndex: number) => string | number;
  onRowClick?: (row: T, rowIndex: number) => void;
  getRowClassName?: (row: T, rowIndex: number) => string | undefined;
  selectAll?: {
    checked: boolean;
    indeterminate: boolean;
    onSelectAll: () => void;
  };
  /**
   * 内置分页（客户端）：true = 每页 10 条；对象可自定义 pageSize。
   * 数据请传入已按期望顺序（如最新在前）排列的数组，第一页即其前 N 条。
   */
  pagination?: boolean | { pageSize?: number };
  /**
   * 受控排序：传入后组件不再自行排序（调用方通常需对全量数据排序后再分页，
   * 否则组件内排序只能作用于传入切片）；不传则使用组件内部排序态。
   */
  sort?: SortState<T> | null;
  onSortChange?: (sort: SortState<T> | null) => void;
}

const DEFAULT_PAGE_SIZE = 10;

const classes = {
  tableHeaderRow: "h-[38px] border-b border-memBorder-primary",
  tableHeaderCell:
    "w-[230px] h-[38px] p-2 align-middle bg-surface-default-secondary text-onSurface-default-secondary",
  tableHeaderCheckCell:
    "w-[40px] h-[38px] p-2 align-middle bg-surface-default-secondary",
  tableHeaderCheckWrap: "flex items-center gap-2",
  tableHeaderCheckBox:
    "box-border flex h-4 w-4 items-center gap-2.5 rounded-sm border border-memBorder-primary p-1",
  tableHeaderDivider:
    "w-px self-stretch border border-memBorder-primary shrink-0",
  tableRow:
    "h-[38px] border-t border-memBorder-primary bg-surface-default-primary hover:bg-surface-default-primary-hover",
  tableCell:
    "text-sm font-normal text-onSurface-default-secondary px-4 py-2.5 justify-start align-middle font-[Fustat] leading-[150%] tracking-normal",
  tableCellFlush: "align-middle",
  tableCellBase: "text-sm px-6",
  tableCellPadding: "",
} as const;

export function DataTable<T>({
  data,
  columns,
  className = "",
  getRowKey,
  onRowClick,
  getRowClassName,
  selectAll,
  pagination,
  sort: controlledSort,
  onSortChange,
}: DataTableProps<T>) {
  const [internalSort, setInternalSort] = useState<SortState<T> | null>(null);
  const [rawPage, setPage] = useState(0);
  // 受控优先：传了 sort/onSortChange 即由调用方负责排序本身
  const isControlledSort = onSortChange !== undefined;
  const sortState = isControlledSort ? (controlledSort ?? null) : internalSort;

  const applySortChange = (next: SortState<T> | null) => {
    if (isControlledSort) onSortChange(next);
    else setInternalSort(next);
    setPage(0);
  };

  const pageSize =
    typeof pagination === "object" && pagination?.pageSize
      ? pagination.pageSize
      : DEFAULT_PAGE_SIZE;
  const paginated = !!pagination;

  // 排序：仅当该列仍声明 sortable 时生效（列定义变化自动失效旧排序态）；
  // 受控模式下调用方已排好序，这里直接透传
  const sortedData = useMemo(() => {
    if (!sortState || isControlledSort) return data;
    const column = columns.find((c) => c.key === sortState.key);
    if (!column?.sortable) return data;
    const valueOf = (row: T): string | number =>
      column.sortValue ? column.sortValue(row) : (row[sortState.key] as string | number);
    const copy = [...data];
    copy.sort((a, b) => {
      const va = valueOf(a);
      const vb = valueOf(b);
      const cmp =
        typeof va === "number" && typeof vb === "number"
          ? va - vb
          : String(va ?? "").localeCompare(String(vb ?? ""), "zh-CN");
      return sortState.direction === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [data, columns, sortState]);

  const pageCount = Math.max(1, Math.ceil(sortedData.length / pageSize));
  const page = Math.min(rawPage, pageCount - 1);
  const visibleData = paginated
    ? sortedData.slice(page * pageSize, (page + 1) * pageSize)
    : sortedData;

  const minHeight =
    visibleData.length > 0
      ? Math.max(76, 38 + visibleData.length * 38)
      : 100;
  // Proportional column widths so table fits container (width numbers treated as relative weights)
  const totalWeight = columns.reduce(
    (sum, col) => sum + (typeof col.width === "number" ? col.width : 100),
    0,
  );

  const toggleSort = (column: Column<T>) => {
    let next: SortState<T> | null;
    if (!sortState || sortState.key !== column.key) {
      next = { key: column.key, direction: "asc" };
    } else if (sortState.direction === "asc") {
      next = { key: column.key, direction: "desc" };
    } else {
      next = null;
    }
    applySortChange(next);
  };

  return (
    <div
      className={`min-w-0 max-w-full overflow-hidden transition-all duration-300 ease-in-out ${className}`}
      style={{ minHeight: `${minHeight}px` }}
    >
      <table className="table-fixed w-full">
        <colgroup>
          {columns.map((col, i) => {
            const weight = typeof col.width === "number" ? col.width : 100;
            const pct =
              totalWeight > 0
                ? (weight / totalWeight) * 100
                : 100 / columns.length;
            return <col key={i} style={{ width: `${pct}%` }} />;
          })}
        </colgroup>
        <thead>
          <tr className={classes.tableHeaderRow}>
            {columns.map((column, index) => {
              const isLastColumn = index === columns.length - 1;

              if (column.headerVariant === "check") {
                return (
                  <th key={index} className={classes.tableHeaderCheckCell}>
                    <div className="flex h-full items-stretch justify-between">
                      <div className={classes.tableHeaderCheckWrap}>
                        {selectAll ? (
                          <Checkbox
                            checked={
                              selectAll.indeterminate
                                ? "indeterminate"
                                : selectAll.checked
                            }
                            onCheckedChange={selectAll.onSelectAll}
                          />
                        ) : (
                          <span className={classes.tableHeaderCheckBox} />
                        )}
                      </div>
                      {!isLastColumn && (
                        <div className={classes.tableHeaderDivider} />
                      )}
                    </div>
                  </th>
                );
              }

              const Icon = column.icon;
              const relevantClasses = column.className
                ? column.className
                    .split(" ")
                    .filter(
                      (c) =>
                        c.startsWith("w-") ||
                        c.startsWith("min-w-") ||
                        c.startsWith("max-w-") ||
                        c.startsWith("text-center") ||
                        c.startsWith("text-left") ||
                        c.startsWith("text-right"),
                    )
                    .join(" ")
                : "";

              const baseHeaderClass = classes.tableHeaderCell;
              const alignClass =
                column.align === "center"
                  ? "text-center"
                  : column.align === "right"
                    ? "text-right"
                    : "";
              const mergedRelevantClasses =
                `${relevantClasses} ${alignClass}`.trim();
              const hasCustomAlignment =
                mergedRelevantClasses.includes("text-center") ||
                mergedRelevantClasses.includes("text-right");
              const headerClassName = hasCustomAlignment
                ? `${baseHeaderClass.replace("text-left", "")} ${mergedRelevantClasses}`.trim()
                : mergedRelevantClasses
                  ? `${baseHeaderClass} ${mergedRelevantClasses}`.trim()
                  : baseHeaderClass;
              const headerCellClassName = `${headerClassName} min-w-0`;

              const flexAlignment = mergedRelevantClasses.includes(
                "text-center",
              )
                ? "justify-center"
                : mergedRelevantClasses.includes("text-right")
                  ? "justify-end"
                  : "";

              const activeSort =
                sortState?.key === column.key ? sortState.direction : null;
              const SortIcon = !column.sortable
                ? null
                : activeSort === "asc"
                  ? ChevronUp
                  : activeSort === "desc"
                    ? ChevronDown
                    : ChevronsUpDown;

              const labelNode = (
                <>
                  {Icon && <Icon className="size-4 shrink-0" />}
                  <span className="truncate font-[Fustat] text-[11px] font-medium uppercase leading-[16px] tracking-[0.02em] text-onSurface-default-tertiary">
                    {column.label}
                  </span>
                  {SortIcon && (
                    <SortIcon
                      className={`size-3 shrink-0 ${
                        activeSort
                          ? "text-onSurface-default-primary"
                          : "text-onSurface-default-tertiary/60"
                      }`}
                    />
                  )}
                </>
              );

              return (
                <th key={index} className={headerCellClassName}>
                  <div className="flex h-full min-w-0 items-stretch justify-between">
                    <div
                      className={`flex min-w-0 flex-1 items-center gap-2 overflow-hidden ${flexAlignment}`}
                    >
                      {column.sortable ? (
                        <button
                          type="button"
                          onClick={() => toggleSort(column)}
                          title={`按${column.label}排序`}
                          className="flex min-w-0 items-center gap-1.5 rounded-sm outline-none hover:text-onSurface-default-secondary focus-visible:ring-1 focus-visible:ring-memBorder-emphasis"
                        >
                          {labelNode}
                        </button>
                      ) : (
                        labelNode
                      )}
                    </div>
                    {!isLastColumn && (
                      <div className={classes.tableHeaderDivider} />
                    )}
                  </div>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody className="transition-all duration-300 ease-in-out">
          {visibleData.map((row, rowIndex) => (
            <tr
              key={getRowKey ? String(getRowKey(row, rowIndex)) : rowIndex}
              className={`${classes.tableRow} ${
                onRowClick ? "cursor-pointer" : ""
              } ${getRowClassName ? (getRowClassName(row, rowIndex) ?? "") : ""} animate-fade-in`}
              onClick={onRowClick ? () => onRowClick(row, rowIndex) : undefined}
            >
              {columns.map((column, colIndex) => {
                const value = row[column.key];
                const baseCellClass =
                  column.cellVariant === "flush"
                    ? classes.tableCellFlush
                    : classes.tableCell;
                const cellClassName = `${column.className || baseCellClass} min-w-0 overflow-hidden`;
                return (
                  <td key={colIndex} className={cellClassName}>
                    <div className="min-w-0 overflow-hidden">
                      {column.render
                        ? column.render(value, row)
                        : String(value)}
                    </div>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {paginated && sortedData.length > 0 && (
        <div className="flex items-center justify-between border-t border-memBorder-primary px-4 py-2 text-xs text-onSurface-default-tertiary">
          <span>
            第 {page * pageSize + 1}–
            {Math.min((page + 1) * pageSize, sortedData.length)} 条，共{" "}
            {sortedData.length} 条
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="xs"
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              上一页
            </Button>
            <span className="flex items-center px-1 tabular-nums">
              {page + 1} / {pageCount}
            </span>
            <Button
              variant="outline"
              size="xs"
              disabled={page >= pageCount - 1}
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
            >
              下一页
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export { classes as tableClasses };
