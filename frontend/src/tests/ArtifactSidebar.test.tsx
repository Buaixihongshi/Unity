import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ArtifactSidebar } from "../components/ArtifactSidebar";
const items = [
  {
    id: "m1:demo.md",
    identity: "m1:demo.md",
    eventId: "e1",
    mission_id: "m1",
    title: "demo.md",
    path: "demo.md",
    kind: "md",
    created_at: "2026-10-31",
  },
];
beforeEach(() => {
  localStorage.removeItem("unity.artifactSidebar.pinned");
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
});
afterEach(() => vi.unstubAllGlobals());
it("retains search when expanding scope and locates an indexed conversation", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        items: [
          {
            ...items[0],
            title: "other.md",
            conversation_id: "c2",
            conversation_title: "第二会话",
            event_id: "e1",
          },
        ],
        next_cursor: null,
      }),
    }),
  );
  const locate = vi.fn();
  render(
    <ArtifactSidebar
      items={items}
      onLocate={locate}
      onPinnedChange={() => {}}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "产物 1" }));
  fireEvent.change(screen.getByLabelText("搜索产物"), {
    target: { value: "other" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "去全部仓库搜「other」" }),
  );
  await screen.findByText("other.md");
  expect(screen.getByLabelText("搜索产物")).toHaveValue("other");
  fireEvent.click(screen.getByRole("button", { name: "第二会话" }));
  expect(locate).toHaveBeenCalledWith(
    expect.objectContaining({ conversation_id: "c2" }),
  );
});
it("persists pinning and gives Escape to the viewer before the drawer", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        path: "demo.md",
        kind: "md",
        content: "hello",
        total_chars: 5,
        truncated: false,
      }),
    }),
  );
  render(
    <ArtifactSidebar
      items={items}
      onLocate={() => undefined}
      onPinnedChange={() => {}}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "产物 1" }));
  fireEvent.click(screen.getByRole("button", { name: "固定" }));
  expect(localStorage.getItem("unity.artifactSidebar.pinned")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "固定" }));
  fireEvent.click(screen.getByRole("button", { name: /demo.md md/ }));
  await screen.findByText("hello");
  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(screen.getByLabelText("交付产物")).toBeVisible();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByLabelText("交付产物")).not.toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "产物 1" })).toHaveFocus(),
  );
});

it("does not report no matches while older records remain and continues scanning", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({ items: [], next_cursor: "2" }),
    })
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        items: [{ ...items[0], event_id: "e1" }],
        next_cursor: null,
      }),
    });
  vi.stubGlobal("fetch", fetch);
  render(
    <ArtifactSidebar
      items={items}
      onLocate={() => undefined}
      onPinnedChange={() => {}}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "产物 1" }));
  fireEvent.click(screen.getByRole("tab", { name: "全部仓库" }));
  await screen.findByText("已扫描较近记录，继续查找");
  expect(screen.queryByText("没有匹配的产物")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
  await screen.findByText("demo.md");
  expect(fetch.mock.calls[1][0]).toContain("cursor=2");
});

it("debounces rapid global search and cancels a pending request on close", async () => {
  vi.useFakeTimers();
  try {
    const fetch = vi
      .fn()
      .mockResolvedValue({
        ok: true,
        json: async () => ({ items: [], next_cursor: null }),
      });
    vi.stubGlobal("fetch", fetch);
    render(
      <ArtifactSidebar
        items={items}
        onLocate={() => undefined}
        onPinnedChange={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "产物 1" }));
    fireEvent.click(screen.getByRole("tab", { name: "全部仓库" }));
    for (const value of ["n", "ne", "needle"]) {
      fireEvent.change(screen.getByLabelText("搜索产物"), {
        target: { value },
      });
      await vi.advanceTimersByTimeAsync(80);
    }
    expect(fetch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(170);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toContain("q=needle");
    fireEvent.change(screen.getByLabelText("搜索产物"), {
      target: { value: "cancel" },
    });
    fireEvent.click(screen.getByRole("button", { name: "收起产物栏" }));
    await vi.advanceTimersByTimeAsync(300);
    expect(fetch).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});
