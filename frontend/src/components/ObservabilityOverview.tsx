import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, ChevronRight, RefreshCw } from "lucide-react";
import { useObservabilityOverview, type ObservabilityWindow } from "../hooks/useObservabilityOverview";
import { fetchObservabilityTurn } from "../lib/api";
import { formatAgentName } from "../lib/missionView";
import type {
  ObservabilityCheck,
  ObservabilityLayer,
  ObservabilityOverviewCheck,
  ObservabilityOverviewPayload,
  ObservabilityOverviewTurn,
  ObservabilityStep,
  ObservabilityTurn,
  ObservabilityTurnDetailPayload,
} from "../types";

const WINDOWS: { id: ObservabilityWindow; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "today", label: "今天" },
  { id: "7d", label: "7 天" },
  { id: "30d", label: "30 天" },
];

const CHECK_LABEL: Record<string, string> = {
  turn_closed: "回合终态",
  blocked_explained: "阻塞说明",
  usage_reported: "用量记录",
  claims_match_events: "声明一致",
  tool_errors_handled: "错误处理",
};

// Older payloads carry no layer; the check name decides.
const TRACE_CHECKS = new Set(["turn_closed", "blocked_explained", "usage_reported"]);

const LAYERS: { id: ObservabilityLayer; label: string; hint: string }[] = [
  { id: "trace", label: "Trace 可信度", hint: "记录本身能不能信：宿主管道的问题" },
  { id: "agent", label: "Agent 行为", hint: "Agent 说的和做的：trace 不可信的回合不评" },
];

const ATTRIBUTION_LABEL: Record<string, string> = {
  environment: "环境",
  user: "用户",
  agent: "Agent",
};

function layerOf(item: { name?: string; check?: string; layer?: ObservabilityLayer }): ObservabilityLayer {
  return item.layer ?? (TRACE_CHECKS.has(item.name ?? item.check ?? "") ? "trace" : "agent");
}

const STATUS_LABEL: Record<string, string> = {
  completed: "完成",
  blocked: "阻塞",
  failed: "失败",
  running: "进行中",
};

const STEP_LABEL: Record<ObservabilityStep["kind"], string> = {
  tool: "工具",
  native: "原生",
  a2a: "协作",
  artifact: "产物",
};

type View = "overview" | "turns" | "turn";

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `$${value.toFixed(2)}`;
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

function count(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

function duration(ms: number): string {
  if (!ms) return "—";
  return ms >= 10_000 ? `${Math.round(ms / 1000)}s` : `${(ms / 1000).toFixed(1)}s`;
}

function statusTone(status: string): string {
  return status === "completed" ? "pass" : status === "failed" ? "fail" : "na";
}

function CheckTag({ check, full = false }: { check: ObservabilityCheck; full?: boolean }) {
  const tone = check.value === 1 ? "pass" : check.value === 0 ? "fail" : "na";
  const label = full ? check.name : check.name.replace(/_match_events|_errors_handled|_explained/, "");
  return (
    <span className={`obsv-tag obsv-tag--${tone}`} title={`${check.name}：${check.note}`}>
      {label}
    </span>
  );
}

function CheckGroup({ layer, rows }: { layer: (typeof LAYERS)[number]; rows: ObservabilityOverviewCheck[] }) {
  const scored = rows.reduce((sum, row) => sum + row.pass + row.fail, 0);
  const passed = rows.reduce((sum, row) => sum + row.pass, 0);
  const failed = rows.reduce((sum, row) => sum + row.fail, 0);
  return (
    <section className="obsv-check-group" aria-label={layer.label}>
      <header><h4>{layer.label}</h4><span>{layer.hint}</span></header>
      <div className="obsv-checks">
        {rows.map((row) => (
          <div key={row.name} className="obsv-check-card" title={row.name}>
            <span>{CHECK_LABEL[row.name] ?? row.name}</span>
            <b>{row.rate === null ? "—" : pct(row.rate)}</b>
            <small>{row.pass} 通过 · {row.fail} 失败 · {row.na} n/a</small>
            {row.health ? (
              <em className="obsv-health" title={row.health}>
                <AlertTriangle size={11} aria-hidden="true" />{row.health}
              </em>
            ) : null}
          </div>
        ))}
        <div className="obsv-check-card obsv-check-card--summary">
          <span>检查通过</span>
          <b>{scored ? `${passed}/${scored}` : "—"}</b>
          <small>{failed} 条失败</small>
        </div>
      </div>
    </section>
  );
}

function WindowChips({ window, onChange }: {
  window: ObservabilityWindow;
  onChange: (next: ObservabilityWindow) => void;
}) {
  return (
    <div className="obsv-windows" role="group" aria-label="时间范围">
      {WINDOWS.map((item) => (
        <button
          key={item.id}
          type="button"
          className={item.id === window ? "is-active" : ""}
          aria-pressed={item.id === window}
          onClick={() => onChange(item.id)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

function Header({ data, window, onWindow, onRefresh, loading, view, onBack }: {
  data: ObservabilityOverviewPayload | null;
  window: ObservabilityWindow;
  onWindow: (next: ObservabilityWindow) => void;
  onRefresh: () => void;
  loading: boolean;
  view: View;
  onBack: () => void;
}) {
  return (
    <div className="obsv-head">
      <div className="obsv-head-title">
        {view !== "overview" ? (
          <button type="button" className="obsv-back" onClick={onBack}>
            <ArrowLeft size={14} aria-hidden="true" /> {view === "turn" ? "返回" : "返回总览"}
          </button>
        ) : null}
        <div>
          <h3 id="settings-observability-title">观测</h3>
          <p>
            {data
              ? `全部会话 · ${data.totals.conversations} 个会话 / ${data.totals.missions} 个任务 / ${data.totals.turns} 个回合`
              : "全部会话"}
          </p>
        </div>
      </div>
      <div className="obsv-head-actions">
        <WindowChips window={window} onChange={onWindow} />
        <button type="button" className="obsv-refresh" onClick={onRefresh}
          aria-label="刷新观测数据" title="刷新">
          <RefreshCw size={15} aria-hidden="true" className={loading ? "obsv-spin" : undefined} />
        </button>
      </div>
    </div>
  );
}

function Overview({ data, onOpenTurns, onOpenTurn }: {
  data: ObservabilityOverviewPayload;
  onOpenTurns: () => void;
  onOpenTurn: (turnId: string) => void;
}) {
  const { totals, coverage, checks, by_agent, by_provider, failures, recent_turns, daily, spend } = data;
  const dailyMax = Math.max(1, ...daily.map((row) => row.turns));

  return (
    <div className="obsv-top">
      <aside className="obsv-rail">
        <div className="obsv-card">
          <h4>总量</h4>
          <div className="obsv-line"><span>回合</span><b>{totals.turns}</b></div>
          <div className="obsv-line"><span>工具调用</span><b>{totals.tool_calls}</b></div>
          <div className="obsv-line"><span>原生调用</span><b>{totals.native_calls}</b></div>
          <div className="obsv-line"><span>Token</span><b>{count(totals.tokens)}</b></div>
          <div className="obsv-line"><span>花费</span><b>{money(spend.total)}</b></div>
        </div>

        <div className="obsv-card">
          <h4>Trace 覆盖</h4>
          {([
            ["原生工具可见", coverage.native_rate],
            ["成本已知", coverage.cost_known_rate],
            ["工具输出", coverage.output_rate],
          ] as const).map(([label, rate]) => (
            <div key={label} className="obsv-cov">
              <div className="obsv-cov-label"><span>{label}</span><b>{pct(rate)}</b></div>
              <div className={`obsv-bar ${rate !== null && rate < 0.3 ? "is-low" : ""}`}>
                <i style={{ width: `${rate === null ? 0 : Math.round(rate * 100)}%` }} />
              </div>
            </div>
          ))}
        </div>

        <div className="obsv-card">
          <h4>近 14 天</h4>
          <div className="obsv-spark" aria-label="近 14 天回合数">
            {daily.map((row) => (
              <i key={row.date} style={{ height: `${Math.round(row.turns / dailyMax * 100)}%` }}
                title={`${row.date} · ${row.turns} 回合`} />
            ))}
          </div>
        </div>
      </aside>

      <div className="obsv-main">
        {LAYERS.map((layer) => {
          const rows = checks.filter((row) => layerOf(row) === layer.id);
          return rows.length ? <CheckGroup key={layer.id} layer={layer} rows={rows} /> : null;
        })}

        <div className="obsv-tables">
          <div className="obsv-panel">
            <h4>Agent</h4>
            <table>
              <thead>
                <tr><th>Agent</th><th>回合</th>
                  <th title="Agent 自身造成的阻塞，含无法归类的">阻塞</th>
                  <th title="网络、鉴权、配置等环境原因 / 用户停止，不计入 Agent">环境/用户</th>
                  <th>工具</th><th>花费</th></tr>
              </thead>
              <tbody>
                {by_agent.map((row) => (
                  <tr key={row.agent}>
                    <td><b>{formatAgentName(row.agent)}</b></td>
                    <td>{row.turns}</td>
                    <td className={row.blocked ? "obsv-bad" : "obsv-muted"}>{row.blocked ?? "—"}</td>
                    <td className="obsv-muted">
                      {row.blocked_environment === undefined ? "—" : `${row.blocked_environment}/${row.blocked_user ?? 0}`}
                    </td>
                    <td>{row.tools}</td>
                    <td>{money(row.cost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="obsv-panel">
            <h4>Provider</h4>
            <table>
              <thead><tr><th>Provider</th><th>回合</th><th>花费</th><th>无价</th></tr></thead>
              <tbody>
                {by_provider.map((row) => (
                  <tr key={row.provider}>
                    <td><b>{row.provider}</b></td>
                    <td>{row.turns}</td>
                    <td>{money(row.usd)}</td>
                    <td className="obsv-muted">{row.unknown}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="obsv-lower">
          <div className="obsv-panel">
            <div className="obsv-panel-head">
              <h4>最近失败</h4>
              <button type="button" onClick={onOpenTurns}>
                全部回合 <ChevronRight size={14} aria-hidden="true" />
              </button>
            </div>
            {failures.length === 0 ? (
              <p className="obsv-empty">没有失败的检查。</p>
            ) : (
              <ul className="obsv-failure-list">
                {failures.slice(0, 3).map((row) => (
                  <li key={`${row.turn_id}-${row.check}`}>
                    <button type="button" className="obsv-failure" onClick={() => onOpenTurn(row.turn_id)}>
                      <strong>
                        <span className={`obsv-layer obsv-layer--${layerOf(row)}`}>
                          {layerOf(row) === "trace" ? "trace" : "agent"}
                        </span>
                        {row.check} · {row.agent}
                      </strong>
                      <span>{row.note}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="obsv-panel">
            <div className="obsv-panel-head">
              <h4>最近回合</h4>
              <button type="button" onClick={onOpenTurns}>
                查看全部 <ChevronRight size={14} aria-hidden="true" />
              </button>
            </div>
            <ul className="obsv-recent-list">
              {recent_turns.slice(0, 4).map((turn) => (
                <li key={turn.turn_id}>
                  <button type="button" onClick={() => onOpenTurn(turn.turn_id)}>
                    <span className="obsv-mono obsv-muted">{(turn.at ?? "").slice(5, 16).replace("T", " ")}</span>
                    <strong>{turn.agent}</strong>
                    <span className="obsv-muted">{turn.provider}</span>
                    <span className={`obsv-tag obsv-tag--${statusTone(turn.status)}`}>
                      {STATUS_LABEL[turn.status] ?? turn.status}
                    </span>
                    <span className="obsv-mono">{money(turn.cost)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

function TurnsTable({ data, onOpenTurn }: {
  data: ObservabilityOverviewPayload;
  onOpenTurn: (turnId: string) => void;
}) {
  const rows: ObservabilityOverviewTurn[] = data.recent_turns;
  return (
    <div className="obsv-detail">
      <div className="obsv-panel">
        <div className="obsv-panel-head"><h4>检查项</h4><span>{data.totals.turns} 回合</span></div>
        <table>
          <thead>
            <tr><th>检查项</th><th>层</th><th>通过</th><th>失败</th><th>n/a</th><th>通过率</th><th>最近失败 / 提示</th></tr>
          </thead>
          <tbody>
            {data.checks.map((row) => (
              <tr key={row.name}>
                <td><b>{row.name}</b></td>
                <td className="obsv-muted">{LAYERS.find((layer) => layer.id === layerOf(row))?.label}</td>
                <td className="obsv-ok">{row.pass}</td>
                <td className={row.fail ? "obsv-bad" : "obsv-muted"}>{row.fail}</td>
                <td className="obsv-muted">{row.na}</td>
                <td>{row.rate === null ? "—" : pct(row.rate)}</td>
                <td className="obsv-muted">
                  {data.failures.find((failure) => failure.check === row.name)?.note ?? row.health ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="obsv-panel">
        <div className="obsv-panel-head"><h4>最近回合</h4><span>最新 {rows.length} 条 · 点击查看细节</span></div>
        <table>
          <thead>
            <tr><th>时间</th><th>Agent</th><th>Provider / 模型</th><th>状态</th>
              <th>耗时</th><th>Tokens</th><th>花费</th><th>检查</th></tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.turn_id} className="obsv-rowlink" tabIndex={0} role="button"
                aria-label={`查看 ${row.agent} 的回合细节`}
                onClick={() => onOpenTurn(row.turn_id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onOpenTurn(row.turn_id);
                  }
                }}>
                <td className="obsv-mono obsv-muted">{(row.at ?? "").slice(5, 16).replace("T", " ")}</td>
                <td><b>{row.agent}</b></td>
                <td>{row.provider}{row.model ? <span className="obsv-muted"> · {row.model}</span> : null}</td>
                <td><span className={`obsv-tag obsv-tag--${statusTone(row.status)}`}>
                  {STATUS_LABEL[row.status] ?? row.status}
                </span></td>
                <td className="obsv-mono">{duration(row.duration_ms)}</td>
                <td className="obsv-mono">{count(row.tokens)}</td>
                <td className="obsv-mono">{money(row.cost)}</td>
                <td>
                  <div className="obsv-tags">{row.checks.map((check) => <CheckTag key={check.name} check={check} />)}</div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="obsv-detail-foot">
        <div className="obsv-panel">
          <h4>花费构成</h4>
          <div className="obsv-line"><span>报价</span><b>{money(data.spend.reported)}</b></div>
          <div className="obsv-line"><span>估算</span><b>{money(data.spend.estimated)}</b></div>
          <div className="obsv-line"><span>无价回合</span><b>{data.spend.unknown_turns}</b></div>
        </div>
        <div className="obsv-panel">
          <h4>Agent</h4>
          {data.by_agent.map((row) => (
            <div key={row.agent} className="obsv-line">
              <span>{formatAgentName(row.agent)}</span><b>{row.turns} 回合 · {money(row.cost)}</b>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function StepRow({ step }: { step: ObservabilityStep }) {
  const detail = step.output || step.summary;
  return (
    <li className={`obsv-step ${step.ok ? "" : "is-failed"}`}>
      <div className="obsv-step-head">
        <span className={`obsv-step-kind obsv-step-kind--${step.kind}`}>{STEP_LABEL[step.kind]}</span>
        <strong>{step.tool || step.kind}</strong>
        {step.phase ? <small>{step.phase}</small> : null}
        {step.ok ? null : <em>失败</em>}
        {step.duration_ms ? <small>{step.duration_ms} ms</small> : null}
      </div>
      {step.error ? <p className="obsv-step-error">{step.error}</p> : null}
      {step.args && Object.keys(step.args).length > 0 ? (
        <pre className="obsv-step-pre">{JSON.stringify(step.args, null, 2)}</pre>
      ) : null}
      {detail ? <pre className="obsv-step-pre">{detail}</pre>
        : <p className="obsv-step-muted">{step.kind === "tool" ? "未记录输出" : "只有调用记录"}</p>}
    </li>
  );
}

function TurnDetail({ detail, loading, error, onRetry }: {
  detail: ObservabilityTurnDetailPayload | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  if (error) {
    return (
      <p className="obsv-error" role="alert">
        {error}
        <button type="button" onClick={onRetry}>重试</button>
      </p>
    );
  }
  if (!detail) return <p className="obsv-loading">{loading ? "正在读取回合…" : "没有这个回合。"}</p>;

  const turn: ObservabilityTurn = detail.turn;
  return (
    <div className="obsv-turn-detail">
      <div className="obsv-panel obsv-turn-meta">
        <div className="obsv-turn-title">
          <strong>{turn.agent_name || turn.agent_id}</strong>
          <span className="obsv-muted">{turn.provider_id}{turn.model ? ` · ${turn.model}` : ""}</span>
          <span className={`obsv-tag obsv-tag--${statusTone(turn.status)}`}>{STATUS_LABEL[turn.status] ?? turn.status}</span>
        </div>
        <div className="obsv-turn-facts">
          <span>{duration(turn.duration_ms)}</span>
          <span>{turn.usage ? `${count(turn.tokens)} tok` : "token 未记录"}</span>
          <span>{money(turn.cost_usd)}</span>
          <span>{turn.steps.length} 步</span>
          {turn.failure_class ? (
            <span title="失败原因分类 · 归属">
              {turn.failure_class} · {ATTRIBUTION_LABEL[turn.attribution ?? ""] ?? "—"}
            </span>
          ) : null}
        </div>
        <p className="obsv-muted obsv-turn-mission">{detail.mission.title}</p>
        <div className="obsv-tags">{turn.checks.map((check) => <CheckTag key={check.name} check={check} full />)}</div>
      </div>

      <ol className="obsv-steps">
        {turn.steps.length === 0
          ? <li className="obsv-step-muted">这个回合没有记录到步骤。</li>
          : turn.steps.map((step) => <StepRow key={step.id} step={step} />)}
      </ol>

      {turn.reply ? (
        <details className="obsv-reply">
          <summary>回复</summary>
          <pre>{turn.reply}</pre>
        </details>
      ) : null}
    </div>
  );
}

/** Settings → 观测: global stats, turn tables and single-turn detail. */
export function ObservabilityOverview() {
  const [window, setWindow] = useState<ObservabilityWindow>("all");
  const [view, setView] = useState<View>("overview");
  const [previous, setPrevious] = useState<View>("overview");
  const [turnId, setTurnId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ObservabilityTurnDetailPayload | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailTick, setDetailTick] = useState(0);
  const latestDetail = useRef(0);
  const { data, loading, error, refresh } = useObservabilityOverview(window);

  useEffect(() => {
    if (!turnId) return;
    const request = ++latestDetail.current;
    setDetailLoading(true);
    setDetailError(null);
    fetchObservabilityTurn(turnId)
      .then((payload) => {
        if (request !== latestDetail.current) return;
        setDetail(payload);
      })
      .catch((err: unknown) => {
        if (request !== latestDetail.current) return;
        setDetailError(err instanceof Error ? err.message : "加载回合详情失败");
      })
      .finally(() => {
        if (request === latestDetail.current) setDetailLoading(false);
      });
  }, [turnId, detailTick]);

  function openTurn(id: string, from: View) {
    setPrevious(from);
    setDetail(null);
    setTurnId(id);
    setView("turn");
  }

  function back() {
    if (view === "turn") {
      setView(previous === "turn" ? "overview" : previous);
      setTurnId(null);
      return;
    }
    setView("overview");
  }

  const content = useMemo(() => {
    if (view === "turn") {
      return (
        <TurnDetail detail={detail} loading={detailLoading} error={detailError}
          onRetry={() => setDetailTick((value) => value + 1)} />
      );
    }
    if (!data) return null;
    return view === "turns"
      ? <TurnsTable data={data} onOpenTurn={(id) => openTurn(id, "turns")} />
      : <Overview data={data} onOpenTurns={() => setView("turns")}
          onOpenTurn={(id) => openTurn(id, "overview")} />;
  }, [data, view, detail, detailLoading, detailError]);

  return (
    <section className="obsv" aria-labelledby="settings-observability-title">
      <Header data={data} window={window} onWindow={setWindow} onRefresh={refresh}
        loading={loading} view={view} onBack={back} />
      {error ? (
        <p className="obsv-error" role="alert">
          {error}
          <button type="button" onClick={refresh}>重试</button>
        </p>
      ) : null}
      {!data && loading && view !== "turn" ? <p className="obsv-loading">正在读取全部会话…</p> : null}
      {content}
    </section>
  );
}
