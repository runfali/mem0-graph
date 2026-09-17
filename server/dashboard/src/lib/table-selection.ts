/**
 * 表格批量选择（本页全选 / 选择全部）的纯逻辑。
 *
 * 独立成模块而不是内联在页面里，是因为页面组件依赖 React/Next，无法在
 * 无测试框架的前端工程里单测；纯函数可以直接用 node:test 跑。
 */

export type RowKey = string | number;

/** 本页全选 / 取消本页：只动可见这一页的 key，其它页的选择原样保留。 */
export function pageSelectAll<T extends RowKey>(
  visibleKeys: T[],
  selected: Set<T>,
  checked: boolean,
): Set<T> {
  const next = new Set(selected);
  for (const key of visibleKeys) {
    if (checked) next.add(key);
    else next.delete(key);
  }
  return next;
}

/** 选择全部：一次性纳入给定 key 集合。 */
export function selectAllKeys<T extends RowKey>(keys: T[]): Set<T> {
  return new Set(keys);
}

/**
 * 批量操作的生效 id：按清单顺序过滤。refetch 后已消失的行不参与删除，
 * 否则残留 id 会让批量接口对 404 反复重试。
 */
export function pruneToAvailable<T extends RowKey>(
  selected: Set<T>,
  available: T[],
): T[] {
  return available.filter((key) => selected.has(key));
}

export interface SelectionSummary {
  headerChecked: boolean;
  headerIndeterminate: boolean;
  allSelected: boolean;
  effectiveCount: number;
}

/** 表头三态 + 批量栏计数：表头只看可见页，计数只看仍存在的行。 */
export function summarizeSelection<T extends RowKey>(
  visibleKeys: T[],
  allKeys: T[],
  selected: Set<T>,
): SelectionSummary {
  const effectiveCount = pruneToAvailable(selected, allKeys).length;
  const visibleSelected = visibleKeys.filter((key) => selected.has(key)).length;
  return {
    headerChecked:
      visibleKeys.length > 0 && visibleSelected === visibleKeys.length,
    headerIndeterminate:
      visibleSelected > 0 && visibleSelected < visibleKeys.length,
    allSelected: allKeys.length > 0 && effectiveCount === allKeys.length,
    effectiveCount,
  };
}
