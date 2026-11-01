import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ArtifactPanel } from "../components/ArtifactPanel";
afterEach(() => vi.unstubAllGlobals());
it("loads the correct content after switching missions with the same path", async () => {
  const fetcher = vi
    .fn()
    .mockImplementation((url: string) =>
      Promise.resolve({
        ok: true,
        json: async () => ({
          path: "same.txt",
          kind: "txt",
          content: url.includes("/m1/") ? "first" : "second",
          truncated: false,
          total_chars: 6,
        }),
      }),
    );
  vi.stubGlobal("fetch", fetcher);
  const artifact = {
    id: "m1:same.txt",
    mission_id: "m1",
    title: "same.txt",
    path: "same.txt",
    created_at: "2026-10-31",
  };
  const view = render(<ArtifactPanel missionId="m1" artifacts={[artifact]} />);
  fireEvent.click(screen.getByRole("button", { name: /same.txt/ }));
  await screen.findByText("first");
  fireEvent.click(screen.getByLabelText("关闭产物查看器"));
  view.rerender(
    <ArtifactPanel
      missionId="m2"
      artifacts={[{ ...artifact, id: "m2:same.txt", mission_id: "m2" }]}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /same.txt/ }));
  await screen.findByText("second");
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
});
