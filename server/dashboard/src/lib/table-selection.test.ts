import { test } from "node:test";
import assert from "node:assert/strict";

import {
  pageSelectAll,
  selectAllKeys,
  pruneToAvailable,
  summarizeSelection,
} from "./table-selection.ts";

const pageA = ["a1", "a2", "a3"];
const pageB = ["b1", "b2"];
const all = [...pageA, ...pageB];

test("page select-all adds only the visible page and keeps other pages selected", () => {
  const afterA = pageSelectAll(pageA, new Set<string>(), true);
  assert.deepEqual([...afterA].sort(), ["a1", "a2", "a3"]);
  const afterB = pageSelectAll(pageB, afterA, true);
  assert.deepEqual([...afterB].sort(), ["a1", "a2", "a3", "b1", "b2"]);
});

test("page deselect removes only the visible page", () => {
  const both = new Set([...pageA, ...pageB]);
  const afterA = pageSelectAll(pageA, both, false);
  assert.deepEqual([...afterA].sort(), ["b1", "b2"]);
});

test("page select-all on an empty visible page is a no-op", () => {
  const sel = new Set(["a1"]);
  assert.deepEqual([...pageSelectAll([], sel, true)], ["a1"]);
  assert.deepEqual([...pageSelectAll([], sel, false)], ["a1"]);
});

test("header is checked only when every visible row is selected", () => {
  const s = summarizeSelection(pageA, all, new Set(["a1", "a2", "a3"]));
  assert.equal(s.headerChecked, true);
  assert.equal(s.headerIndeterminate, false);
});

test("header is indeterminate when only some visible rows are selected", () => {
  const s = summarizeSelection(pageA, all, new Set(["a1"]));
  assert.equal(s.headerChecked, false);
  assert.equal(s.headerIndeterminate, true);
});

test("header is unchecked when no visible row is selected", () => {
  const s = summarizeSelection(pageA, all, new Set());
  assert.equal(s.headerChecked, false);
  assert.equal(s.headerIndeterminate, false);
});

test("header state ignores selections made on other pages", () => {
  const s = summarizeSelection(pageA, all, new Set(["b1", "b2"]));
  assert.equal(s.headerChecked, false);
  assert.equal(s.headerIndeterminate, false);
  // 其它页的选择不计入本页表头，但仍是有效选择、仍计入批量计数
  assert.equal(s.effectiveCount, 2);
});

test("effective count ignores ids that left the list after a refetch", () => {
  const s = summarizeSelection(pageA, all, new Set(["a1", "gone"]));
  assert.equal(s.effectiveCount, 1);
  assert.equal(s.allSelected, false);
});

test("allSelected is true only when the whole list is selected", () => {
  assert.equal(summarizeSelection(pageA, all, new Set(all)).allSelected, true);
  assert.equal(
    summarizeSelection(pageA, all, new Set(pageA)).allSelected,
    false,
  );
});

test("allSelected is false for an empty list", () => {
  assert.equal(summarizeSelection([], [], new Set()).allSelected, false);
});

test("selectAllKeys dedupes", () => {
  assert.deepEqual([...selectAllKeys(["x", "x", "y"])].sort(), ["x", "y"]);
});

test("pruneToAvailable keeps list order and drops vanished ids", () => {
  const sel = new Set(["a3", "gone", "a1"]);
  assert.deepEqual(pruneToAvailable(sel, all), ["a1", "a3"]);
});

test("pruneToAvailable returns empty for an empty list", () => {
  assert.deepEqual(pruneToAvailable(new Set(["a1"]), []), []);
});
