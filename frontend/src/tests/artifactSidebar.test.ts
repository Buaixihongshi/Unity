import { expect, it } from "vitest";
import {
  collectConversationArtifacts,
  filterArtifacts,
} from "../lib/artifactSidebar";
import type { ThreadEvent } from "../types";
it("keeps different missions and latest duplicate identity, searches title/path", () => {
  const event = (mid: string, title: string): ThreadEvent => ({
    id: `artifact:${mid}:same.md`,
    mission_id: mid,
    event_type: "artifact.saved",
    role: "system",
    content: "saved",
    created_at: "2026-10-31",
    artifact: {
      id: `${mid}:same.md`,
      mission_id: mid,
      path: "same.md",
      title,
      created_at: "2026-10-31",
      kind: "md",
    },
  });
  const result = collectConversationArtifacts([
    event("m1", "old"),
    event("m2", "second"),
    event("m1", "new"),
  ]);
  expect(result).toHaveLength(2);
  expect(result[0].title).toBe("new");
  expect(filterArtifacts(result, "SAME", "all")).toHaveLength(2);
  expect(filterArtifacts(result, "new", "md")).toHaveLength(1);
  expect(filterArtifacts(result, "", "html")).toHaveLength(0);
});
