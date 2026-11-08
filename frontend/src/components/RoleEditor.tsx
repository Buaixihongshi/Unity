import { useState, type FormEvent } from "react";
import type { CustomAgentInput, HubSettingsAgentView } from "../types";
import { EffortSelect, effortAfterModelChange } from "./EffortSelect";

export interface RoleRouteOption {
  id: string;
  label: string;
  /** API connections need an explicit model ID; CLI connections may use their default. */
  requiresModel: boolean;
  disabled: boolean;
}

interface Props {
  /** The custom role being edited; omitted when adding a new one. */
  agent?: HubSettingsAgentView;
  routeOptions: RoleRouteOption[];
  modelSuggestions: (connectionId: string) => string[];
  /** Effort levels for a connection and model; empty when it has no effort setting. */
  effortChoices: (connectionId: string, model: string) => string[];
  onSubmit: (input: CustomAgentInput) => Promise<void>;
  onCancel: () => void;
}

function splitAliases(raw: string): string[] {
  return raw.split(/[\s,，、]+/).map((alias) => alias.replace(/^@+/, "").trim()).filter(Boolean);
}

/** Form for a user-created role: identity, duties and its own route. */
export function RoleEditor({ agent, routeOptions, modelSuggestions, effortChoices, onSubmit, onCancel }: Props) {
  const firstRoutable = routeOptions.find((option) => !option.disabled)?.id ?? routeOptions[0]?.id ?? "";
  const [displayName, setDisplayName] = useState(agent?.display_name ?? "");
  const [role, setRole] = useState(agent?.role ?? "");
  const [persona, setPersona] = useState(agent?.persona ?? "");
  const [duties, setDuties] = useState((agent?.trigger_points ?? []).join("\n"));
  const [aliases, setAliases] = useState((agent?.aliases ?? []).join("、"));
  const [primary, setPrimary] = useState(agent?.default_provider_id.value ?? firstRoutable);
  const [fallbacks, setFallbacks] = useState<string[]>(agent?.fallback_provider_ids.value ?? []);
  const [models, setModels] = useState<Record<string, string>>(agent?.provider_models?.value ?? {});
  const [efforts, setEfforts] = useState<Record<string, string>>(agent?.provider_efforts?.value ?? {});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const formId = agent ? `role-editor-${agent.id}` : "role-editor-new";
  const optionById = new Map(routeOptions.map((option) => [option.id, option]));
  const routed = [primary, ...fallbacks.filter((id) => id !== primary)];
  const missingModel = routed.find((id) => optionById.get(id)?.requiresModel && !models[id]?.trim());
  const incomplete = !displayName.trim() || !role.trim() || !persona.trim() || !primary;

  function toggleFallback(id: string) {
    setFallbacks((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));
  }

  function setModel(id: string, value: string) {
    setModels((prev) => ({ ...prev, [id]: value }));
    setEfforts((prev) => ({
      ...prev,
      [id]: effortAfterModelChange(prev[id] ?? "", effortChoices(id, value.trim())),
    }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (incomplete || missingModel) return;
    const providerModels: Record<string, string> = {};
    for (const id of routed) {
      const model = models[id]?.trim();
      if (model) providerModels[id] = model;
    }
    const providerEfforts: Record<string, string> = {};
    for (const id of routed) {
      if (efforts[id]) providerEfforts[id] = efforts[id];
    }
    setBusy(true);
    setError(null);
    try {
      await onSubmit({
        display_name: displayName.trim(),
        role: role.trim(),
        persona: persona.trim(),
        aliases: splitAliases(aliases),
        trigger_points: duties.split("\n").map((line) => line.trim()).filter(Boolean),
        default_provider_id: primary,
        fallback_provider_ids: fallbacks.filter((id) => id !== primary),
        provider_models: providerModels,
        provider_efforts: providerEfforts,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存角色失败");
      setBusy(false);
    }
  }

  function renderModelInput(id: string) {
    const option = optionById.get(id);
    const suggestions = modelSuggestions(id);
    const listId = suggestions.length > 0 ? `${formId}-models-${id}` : undefined;
    const missing = Boolean(option?.requiresModel) && !models[id]?.trim();
    return (
      <div className="model-effort-group">
        <input
          type="text"
          className={`custom-input${missing ? " is-invalid" : ""}`}
          aria-label={`${option?.label ?? id} 模型`}
          aria-invalid={missing || undefined}
          list={listId}
          value={models[id] ?? ""}
          placeholder={option?.requiresModel ? "必填：Model ID" : "沿用 CLI 默认"}
          onChange={(e) => setModel(id, e.target.value)}
        />
        {listId && (
          <datalist id={listId}>
            {suggestions.map((model) => <option key={model} value={model} />)}
          </datalist>
        )}
        <EffortSelect
          label={`${option?.label ?? id} 推理强度`}
          value={efforts[id] ?? ""}
          choices={effortChoices(id, models[id]?.trim() ?? "")}
          onChange={(value) => setEfforts((prev) => ({ ...prev, [id]: value }))}
        />
      </div>
    );
  }

  return (
    <form
      className="role-editor"
      aria-labelledby={`${formId}-title`}
      data-testid="role-editor"
      onSubmit={handleSubmit}
    >
      <div className="role-editor-heading">
        <h4 id={`${formId}-title`}>{agent ? `编辑角色 · ${agent.display_name}` : "新增角色"}</h4>
        <p className="settings-section-hint">
          自定义角色保存在本机设置中，下一条消息起即可 @ 点名，也可被其他角色接力。
        </p>
      </div>

      <div className="role-editor-grid">
        <label className="form-group">
          <span className="form-label">名称</span>
          <input
            className="custom-input"
            value={displayName}
            maxLength={24}
            required
            placeholder="如：陆玲珑"
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </label>
        <label className="form-group">
          <span className="form-label">
            别名 <span className="desc-tag">用于 @ 点名，空格或顿号分隔</span>
          </span>
          <input
            className="custom-input"
            value={aliases}
            placeholder="如：玲珑、linglong"
            onChange={(e) => setAliases(e.target.value)}
          />
        </label>
        <label className="form-group role-editor-wide">
          <span className="form-label">职位</span>
          <input
            className="custom-input"
            value={role}
            maxLength={80}
            required
            placeholder="一句话说明主要职责，如：测试工程师和质量把关者"
            onChange={(e) => setRole(e.target.value)}
          />
        </label>
        <label className="form-group role-editor-wide">
          <span className="form-label">人格与交流习惯</span>
          <textarea
            className="custom-input"
            rows={2}
            value={persona}
            maxLength={600}
            required
            placeholder="说话方式、关注点、语气，如：细致耐心，先复现再下结论。"
            onChange={(e) => setPersona(e.target.value)}
          />
        </label>
        <label className="form-group role-editor-wide">
          <span className="form-label">
            职责指引 <span className="desc-tag">可选，每行一条，最多 8 条</span>
          </span>
          <textarea
            className="custom-input"
            rows={3}
            value={duties}
            placeholder="如：先写出复现步骤，再给修复建议。"
            onChange={(e) => setDuties(e.target.value)}
          />
        </label>
        <label className="form-group">
          <span className="form-label">主用连接</span>
          <select
            className="custom-select"
            value={primary}
            onChange={(e) => {
              setPrimary(e.target.value);
              setFallbacks((prev) => prev.filter((id) => id !== e.target.value));
            }}
          >
            {routeOptions.map((option) => (
              <option key={option.id} value={option.id} disabled={option.disabled}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <div className="form-group">
          <span className="form-label">模型</span>
          {primary ? renderModelInput(primary) : null}
        </div>
        <fieldset className="role-fallbacks role-editor-wide">
          <legend>备用连接</legend>
          <div className="fallback-options-grid">
            {routeOptions
              .filter((option) => option.id !== primary)
              .map((option) => (
                <label key={option.id} className="fallback-checkbox-label">
                  <input
                    type="checkbox"
                    checked={fallbacks.includes(option.id)}
                    disabled={option.disabled && !fallbacks.includes(option.id)}
                    onChange={() => toggleFallback(option.id)}
                  />
                  <span>{option.label}</span>
                </label>
              ))}
          </div>
        </fieldset>
        {fallbacks.length > 0 && (
          <div className="role-fallback-models role-editor-wide">
            {fallbacks.map((id) => (
              <div className="form-group" key={id}>
                <span className="form-label">{optionById.get(id)?.label ?? id} 模型</span>
                {renderModelInput(id)}
              </div>
            ))}
          </div>
        )}
      </div>

      {fallbacks.length > 0 && (
        <p className="outbound-tip">
          新的备用连接需要在角色表展开后重新勾选“允许备用连接接收当前任务上下文”。
        </p>
      )}
      {error && (
        <p className="role-editor-error" role="alert">
          {error}
        </p>
      )}
      <div className="role-editor-actions">
        <button type="button" className="settings-agent-reset" onClick={onCancel} disabled={busy}>
          取消
        </button>
        <button
          type="submit"
          className="role-editor-submit"
          disabled={busy || incomplete || Boolean(missingModel)}
        >
          {busy ? "保存中…" : agent ? "保存角色" : "添加角色"}
        </button>
      </div>
    </form>
  );
}
