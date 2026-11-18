import type { ConnectionCatalogView } from "../types";

const EFFORT_LABELS: Record<string, string> = {
  minimal: "minimal · 极简",
  low: "low · 轻度",
  medium: "medium · 均衡",
  high: "high · 深度",
  xhigh: "xhigh · 高阶",
  max: "max · 极限",
  ultra: "ultra · 超限",
};

/**
 * Effort levels to offer for a connection and model. `levels` are what the CLI
 * accepts; a listed model that reports its own levels (possibly none, when the
 * model fixes its effort) narrows them, and with no model chosen the union of
 * listed models applies. Empty means no separate effort setting.
 */
export function effortChoices(
  levels: string[],
  catalog: ConnectionCatalogView | undefined,
  model: string,
): string[] {
  const models = catalog?.models ?? [];
  const chosen = models.find((item) => item.id === model);
  if (chosen?.efforts) return chosen.efforts;
  if (!model && models.some((item) => item.efforts?.length)) {
    return levels.filter((level) => models.some((item) => item.efforts?.includes(level)));
  }
  return levels;
}

// Antigravity names a model's fixed effort as its suffix and then rejects
// --effort; this holds before the model listing arrives (issue #132).
const FIXED_EFFORT_SUFFIX = /-(?:minimal|low|medium|high|xhigh|max)$/;

/** Whether a model already fixes its own effort, so no separate one can be set. */
export function hasFixedEffort(providerId: string, model: string): boolean {
  return providerId === "antigravity" && FIXED_EFFORT_SUFFIX.test(model);
}

/** Keep an effort only while the newly chosen model still supports it. */
export function effortAfterModelChange(effort: string, choices: string[]): string {
  return effort && !choices.includes(effort) ? "" : effort;
}

interface Props {
  label: string;
  value: string;
  choices: string[];
  onChange: (effort: string) => void;
}

/** Compact effort picker that sits beside a model field; empty keeps the CLI default. */
export function EffortSelect({ label, value, choices, onChange }: Props) {
  const unsupported = choices.length === 0;
  const known = choices.includes(value);
  return (
    <select
      className={`effort-select${value ? " is-active" : ""}`}
      aria-label={label}
      title={unsupported ? "该连接或所选模型不能单独设置推理强度" : "推理强度，默认沿用 CLI 自身设置"}
      disabled={unsupported && !value}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">{unsupported && !value ? "强度：不可调" : "强度：默认"}</option>
      {value && !known && <option value={value}>{EFFORT_LABELS[value] ?? value}（当前不可用）</option>}
      {choices.map((effort) => (
        <option key={effort} value={effort}>
          {EFFORT_LABELS[effort] ?? effort}
        </option>
      ))}
    </select>
  );
}
