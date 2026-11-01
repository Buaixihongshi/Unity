import type { Artifact, ThreadEvent } from "../types";
export interface SidebarArtifact extends Artifact {
  identity: string;
  eventId: string | number;
  conversation_id?: string;
  conversation_title?: string;
}
/** Consume the same merged, visibility-filtered timeline as chat cards. */
export function collectConversationArtifacts(
  events: ThreadEvent[],
): SidebarArtifact[] {
  const items = new Map<string, SidebarArtifact>();
  for (const event of events)
    if (event.artifact) {
      items.set(event.artifact.id, {
        ...event.artifact,
        identity: event.artifact.id,
        eventId: event.id,
      });
    }
  return [...items.values()].sort(
    (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at),
  );
}
export function filterArtifacts(
  items: SidebarArtifact[],
  query: string,
  kind: string,
) {
  const q = query.trim().toLocaleLowerCase();
  return items.filter(
    (item) =>
      (kind === "all" || (item.kind ?? "file") === kind) &&
      `${item.title}\n${item.path ?? ""}`.toLocaleLowerCase().includes(q),
  );
}
export function artifactKindGroups(items: SidebarArtifact[]) {
  const counts = new Map<string, number>();
  for (const item of items)
    counts.set(item.kind ?? "file", (counts.get(item.kind ?? "file") ?? 0) + 1);
  return [...counts].map(([kind, count]) => ({ kind, count }));
}
