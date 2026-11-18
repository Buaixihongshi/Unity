import { useState } from "react";
import type { HealthView, OnboardingProviderStatus, OnboardingView } from "../types";

export interface OnboardingCardProps {
  health: HealthView | null;
  healthLoading: boolean;
  healthError: string | null;
  onboarding: OnboardingView | null;
  actionBusy: boolean;
  actionError: string | null;
  onStart: (providerId: string) => void;
  onRetryHealth: () => void;
  onComplete: () => void;
  onSkipRealCli: () => void;
  onUseProviderForTeam: (providerId: string) => void;
  onRefreshProviders: () => void;
  onVerifyProvider: (providerId: string) => void;
  onActivateProvider: (providerId: string) => void;
  onOpenTeamSettings?: () => void;
}

export function OnboardingCard(props: OnboardingCardProps) {
  const [copiedCommand, setCopiedCommand] = useState<string | null>(null);

  function copyCommand(key: string, command?: string | null) {
    if (!command) return;
    void navigator.clipboard?.writeText(command);
    setCopiedCommand(key);
  }

  if (props.healthLoading || (!props.health && !props.healthError)) {
    return (
      <section className="onboarding-card onboarding-card--loading" aria-label="启动引导" aria-live="polite">
        <div className="onboarding-mobius-wrap" aria-hidden="true">
          <svg className="onboarding-mobius-svg" viewBox="0 0 512 512">
            <path className="mobius-depth-shadow"
              d="M298.1,294.9C294.3,291.9 290.6,288.7 286.9,285.6C283.3,282.4 279.8,279.2 276.3,275.9C272.8,272.6 269.4,269.3 266,266C262.7,262.7 259.3,259.3 256,256C252.7,252.7 249.3,249.3 246,246C242.6,242.7 239.2,239.4 235.7,236.1C232.2,232.8 228.7,229.6 225.1,226.4C221.4,223.3 217.7,220.1 213.9,217.1" />
            <path className="mobius-track-loop" pathLength="1000"
              d="M256,256C252.7,252.7 249.3,249.3 246,246C242.6,242.7 239.2,239.4 235.7,236.1C232.2,232.8 228.7,229.6 225.1,226.4C221.4,223.3 217.7,220.1 213.9,217.1C210,214.1 206.1,211.1 201.9,208.3C197.8,205.5 193.5,202.8 189,200.3C184.5,197.8 179.9,195.4 175,193.4C170.1,191.3 165.1,189.5 159.8,188C154.6,186.5 149.1,185.3 143.6,184.7C138,184 132.2,183.7 126.4,184C120.6,184.3 114.6,185.1 108.8,186.6C103,188.1 97.1,190.2 91.6,193.1C86.1,195.9 80.7,199.5 76,203.7C71.2,208 66.8,213 63.3,218.4C59.7,223.8 56.8,230 54.9,236.3C53,242.6 52,249.4 52,256C52,262.6 53,269.4 54.9,275.7C56.8,282 59.7,288.2 63.3,293.6C66.8,299 71.2,304 76,308.3C80.7,312.5 86.1,316.1 91.6,318.9C97.1,321.8 103,323.9 108.8,325.4C114.6,326.9 120.6,327.7 126.4,328C132.2,328.3 138,328 143.6,327.3C149.1,326.7 154.6,325.5 159.8,324C165.1,322.5 170.1,320.7 175,318.6C179.9,316.6 184.5,314.2 189,311.7C193.5,309.2 197.8,306.5 201.9,303.7C206.1,300.9 210,297.9 213.9,294.9C217.7,291.9 221.4,288.7 225.1,285.6C228.7,282.4 232.2,279.2 235.7,275.9C239.2,272.6 242.6,269.3 246,266C249.3,262.7 252.7,259.3 256,256C259.3,252.7 262.7,249.3 266,246C269.4,242.7 272.8,239.4 276.3,236.1C279.8,232.8 283.3,229.6 286.9,226.4C290.6,223.3 294.3,220.1 298.1,217.1C302,214.1 305.9,211.1 310.1,208.3C314.2,205.5 318.5,202.8 323,200.3C327.5,197.8 332.1,195.4 337,193.4C341.9,191.3 346.9,189.5 352.2,188C357.4,186.5 362.9,185.3 368.4,184.7C374,184 379.8,183.7 385.6,184C391.4,184.3 397.4,185.1 403.2,186.6C409,188.1 414.9,190.2 420.4,193.1C425.9,195.9 431.3,199.5 436,203.7C440.8,208 445.2,213 448.7,218.4C452.3,223.8 455.2,230 457.1,236.3C459,242.6 460,249.4 460,256C460,262.6 459,269.4 457.1,275.7C455.2,282 452.3,288.2 448.7,293.6C445.2,299 440.8,304 436,308.3C431.3,312.5 425.9,316.1 420.4,318.9C414.9,321.8 409,323.9 403.2,325.4C397.4,326.9 391.4,327.7 385.6,328C379.8,328.3 374,328 368.4,327.3C362.9,326.7 357.4,325.5 352.2,324C346.9,322.5 341.9,320.7 337,318.6C332.1,316.6 327.5,314.2 323,311.7C318.5,309.2 314.2,306.5 310.1,303.7C305.9,300.9 302,297.9 298.1,294.9C294.3,291.9 290.6,288.7 286.9,285.6C283.3,282.4 279.8,279.2 276.3,275.9C272.8,272.6 269.4,269.3 266,266C262.7,262.7 259.3,259.3 256,256Z" />
            <path className="mobius-cross-cap"
              d="M298.1,294.9C294.3,291.9 290.6,288.7 286.9,285.6C283.3,282.4 279.8,279.2 276.3,275.9C272.8,272.6 269.4,269.3 266,266C262.7,262.7 259.3,259.3 256,256C252.7,252.7 249.3,249.3 246,246C242.6,242.7 239.2,239.4 235.7,236.1C232.2,232.8 228.7,229.6 225.1,226.4C221.4,223.3 217.7,220.1 213.9,217.1" />
            <path className="mobius-cruise-flow" pathLength="1000"
              d="M256,256C252.7,252.7 249.3,249.3 246,246C242.6,242.7 239.2,239.4 235.7,236.1C232.2,232.8 228.7,229.6 225.1,226.4C221.4,223.3 217.7,220.1 213.9,217.1C210,214.1 206.1,211.1 201.9,208.3C197.8,205.5 193.5,202.8 189,200.3C184.5,197.8 179.9,195.4 175,193.4C170.1,191.3 165.1,189.5 159.8,188C154.6,186.5 149.1,185.3 143.6,184.7C138,184 132.2,183.7 126.4,184C120.6,184.3 114.6,185.1 108.8,186.6C103,188.1 97.1,190.2 91.6,193.1C86.1,195.9 80.7,199.5 76,203.7C71.2,208 66.8,213 63.3,218.4C59.7,223.8 56.8,230 54.9,236.3C53,242.6 52,249.4 52,256C52,262.6 53,269.4 54.9,275.7C56.8,282 59.7,288.2 63.3,293.6C66.8,299 71.2,304 76,308.3C80.7,312.5 86.1,316.1 91.6,318.9C97.1,321.8 103,323.9 108.8,325.4C114.6,326.9 120.6,327.7 126.4,328C132.2,328.3 138,328 143.6,327.3C149.1,326.7 154.6,325.5 159.8,324C165.1,322.5 170.1,320.7 175,318.6C179.9,316.6 184.5,314.2 189,311.7C193.5,309.2 197.8,306.5 201.9,303.7C206.1,300.9 210,297.9 213.9,294.9C217.7,291.9 221.4,288.7 225.1,285.6C228.7,282.4 232.2,279.2 235.7,275.9C239.2,272.6 242.6,269.3 246,266C249.3,262.7 252.7,259.3 256,256C259.3,252.7 262.7,249.3 266,246C269.4,242.7 272.8,239.4 276.3,236.1C279.8,232.8 283.3,229.6 286.9,226.4C290.6,223.3 294.3,220.1 298.1,217.1C302,214.1 305.9,211.1 310.1,208.3C314.2,205.5 318.5,202.8 323,200.3C327.5,197.8 332.1,195.4 337,193.4C341.9,191.3 346.9,189.5 352.2,188C357.4,186.5 362.9,185.3 368.4,184.7C374,184 379.8,183.7 385.6,184C391.4,184.3 397.4,185.1 403.2,186.6C409,188.1 414.9,190.2 420.4,193.1C425.9,195.9 431.3,199.5 436,203.7C440.8,208 445.2,213 448.7,218.4C452.3,223.8 455.2,230 457.1,236.3C459,242.6 460,249.4 460,256C460,262.6 459,269.4 457.1,275.7C455.2,282 452.3,288.2 448.7,293.6C445.2,299 440.8,304 436,308.3C431.3,312.5 425.9,316.1 420.4,318.9C414.9,321.8 409,323.9 403.2,325.4C397.4,326.9 391.4,327.7 385.6,328C379.8,328.3 374,328 368.4,327.3C362.9,326.7 357.4,325.5 352.2,324C346.9,322.5 341.9,320.7 337,318.6C332.1,316.6 327.5,314.2 323,311.7C318.5,309.2 314.2,306.5 310.1,303.7C305.9,300.9 302,297.9 298.1,294.9C294.3,291.9 290.6,288.7 286.9,285.6C283.3,282.4 279.8,279.2 276.3,275.9C272.8,272.6 269.4,269.3 266,266C262.7,262.7 259.3,259.3 256,256Z" />
          </svg>
        </div>
        <p className="onboarding-kicker">首次启动</p>
        <h2>正在确认 Unity 状态…</h2>
        <p>马上就好，我们在检查聊天室和本机可用的 CLI。</p>
      </section>
    );
  }

  if (props.healthError || !props.health) {
    return (
      <section className="onboarding-card onboarding-card--error" aria-label="启动引导">
        <p className="onboarding-kicker">还差一步</p>
        <h2>还没有连接到 Unity 后端</h2>
        <p>请回到启动项目的终端，查看第一条错误信息，然后重新连接。</p>
        {props.healthError ? <p role="alert" className="onboarding-error">{props.healthError}</p> : null}
        <button type="button" onClick={props.onRetryHealth}>重新连接</button>
      </section>
    );
  }

  const state = props.onboarding?.state ?? "ready_for_first_chat";
  const providers = props.onboarding?.providers ?? [];
  const labelOf = (providerId?: string | null) =>
    providers.find((provider) => provider.id === providerId)?.label || "真实 CLI";
  const currentProvider = providers.find(
    (provider) => provider.id === props.onboarding?.verification_provider_id,
  );
  const activatedProviderId = props.onboarding?.activated_provider_id;
  const isExtensionVerification = Boolean(
    activatedProviderId
    && currentProvider
    && currentProvider.id !== activatedProviderId,
  );

  if (state === "first_chat_running") {
    return (
      <section className="onboarding-card" aria-label="启动引导" aria-live="polite">
        <p className="onboarding-kicker">第 1 步，共 2 步</p>
        <h2>张楚岚正在通过 {labelOf(props.onboarding?.first_chat_provider_id)} 回复…</h2>
        <p>这次只使用你选择的 CLI，不会回退到其他连接。</p>
        <p className="onboarding-cost-note" role="note">本次对话正在使用你的 CLI 账号发起 1 次模型请求，可能计入套餐额度或产生费用。</p>
      </section>
    );
  }

  if (state === "provider_verifying") {
    return (
      <section className="onboarding-card" aria-label="启动引导" aria-live="polite">
        <p className="onboarding-kicker">正在验证真实 CLI</p>
        <h2>等待 {currentProvider?.label || "真实 CLI"} 通过 MCP 回一句话…</h2>
        <p>这次验证只允许所选 CLI，不会回退到其他连接。验证完成前，团队设置不会改变。</p>
        <p className="onboarding-cost-note" role="note">本次验证正在使用你的 CLI 账号发起 1 次模型请求，可能计入套餐额度或产生费用。</p>
      </section>
    );
  }

  if (state === "provider_verified" && currentProvider && isExtensionVerification) {
    return (
      <section className="onboarding-card onboarding-card--success" aria-label="启动引导">
        <p className="onboarding-kicker">扩展 CLI 验证成功</p>
        <h2>{currentProvider.label} 已经能够通过 Unity 发言</h2>
        <p>原有 {labelOf(activatedProviderId)} 配置没有改变。现在可以在团队配置中，把它分配给张楚岚、冯宝宝或王也。</p>
        <div className="onboarding-actions">
          <button
            type="button"
            onClick={props.onOpenTeamSettings ?? props.onComplete}
            disabled={props.actionBusy}
          >
            完成并打开团队配置
          </button>
          <button type="button" className="button-secondary" onClick={props.onComplete} disabled={props.actionBusy}>
            稍后配置
          </button>
        </div>
      </section>
    );
  }

  if (state === "provider_verification_failed" && currentProvider && isExtensionVerification) {
    return (
      <section className="onboarding-card onboarding-card--error" aria-label="启动引导">
        <p className="onboarding-kicker">扩展 CLI 验证未通过</p>
        <h2>{currentProvider.label} 没有通过 Unity 发言</h2>
        <p>原有 {labelOf(activatedProviderId)} 配置没有改变。检查 CLI 的安装和登录状态后可以重试。</p>
        {props.onboarding?.verification_error ? (
          <p role="alert" className="onboarding-error">{props.onboarding.verification_error}</p>
        ) : null}
        <div className="onboarding-provider-list onboarding-provider-list--primary">
          <ProviderSetupCard
            provider={currentProvider}
            busy={props.actionBusy}
            copiedCommand={copiedCommand}
            onCopy={copyCommand}
            onAction={props.onVerifyProvider}
            actionLabel={`我已登录，重新验证 ${currentProvider.label}`}
          />
        </div>
        <div className="onboarding-actions">
          <button type="button" className="button-secondary" onClick={props.onComplete} disabled={props.actionBusy}>
            稍后再说
          </button>
        </div>
        {props.actionError ? <p role="alert" className="onboarding-error">{props.actionError}</p> : null}
      </section>
    );
  }

  const connectedProviderId = state === "first_chat_succeeded"
    ? props.onboarding?.first_chat_provider_id
    : state === "provider_verified"
      ? currentProvider?.id
      : null;
  if (connectedProviderId) {
    const label = labelOf(connectedProviderId);
    return (
      <section className="onboarding-card onboarding-card--success" aria-label="启动引导">
        <p className="onboarding-kicker">第 2 步，共 2 步 · {label} 已接通</p>
        <h2>{state === "first_chat_succeeded" ? "张楚岚已经完成第一次真实对话" : `${label} 已经能够通过 Unity 发言`}</h2>
        <p>接下来让三位角色都使用 {label}，并请宝儿姐和王也各自打个招呼。之后可以在团队配置里为每位角色换用其他连接。</p>
        <div className="onboarding-actions">
          <button type="button" onClick={() => props.onActivateProvider(connectedProviderId)} disabled={props.actionBusy}>
            {props.actionBusy ? "正在召集团队…" : `让三位角色使用 ${label} 并认识团队`}
          </button>
          <button
            type="button"
            className="button-secondary"
            onClick={() => props.onUseProviderForTeam(connectedProviderId)}
            disabled={props.actionBusy}
          >
            只配置角色，跳过介绍
          </button>
        </div>
        <p className="onboarding-cost-note" role="note">宝儿姐和王也的介绍会再使用 {label} 发起 2 次真实模型请求，可能计入套餐额度或产生费用。</p>
        {props.actionError ? <p role="alert" className="onboarding-error">{props.actionError}</p> : null}
      </section>
    );
  }

  if (state === "team_intro_running") {
    return (
      <section className="onboarding-card" aria-label="启动引导" aria-live="polite">
        <p className="onboarding-kicker">最后一步</p>
        <h2>宝儿姐和王也正在进场…</h2>
        <p>他们会通过 {labelOf(activatedProviderId)} 各用一句话介绍自己。</p>
        <p className="onboarding-cost-note" role="note">这一步使用你的 CLI 账号发起 2 次模型请求，可能计入套餐额度或产生费用。</p>
      </section>
    );
  }

  if (state === "team_intro_failed") {
    const providerId = activatedProviderId || props.onboarding?.verification_provider_id;
    return (
      <section className="onboarding-card onboarding-card--error" aria-label="启动引导">
        <p className="onboarding-kicker">团队介绍未完成</p>
        <h2>宝儿姐或王也刚才没有成功发言</h2>
        <p>三位角色已经切换到 {labelOf(providerId)}，可以重新请他们介绍自己。</p>
        <div className="onboarding-actions">
          {providerId ? (
            <button type="button" onClick={() => props.onActivateProvider(providerId)} disabled={props.actionBusy}>
              {props.actionBusy ? "正在重试…" : "重新介绍团队"}
            </button>
          ) : null}
          <button type="button" className="button-secondary" onClick={props.onComplete} disabled={props.actionBusy}>
            跳过介绍
          </button>
        </div>
        {props.actionError ? <p role="alert" className="onboarding-error">{props.actionError}</p> : null}
      </section>
    );
  }

  if (state === "team_intro_completed") {
    const providerLabel = labelOf(activatedProviderId);
    const expansionProviders = providers.filter((provider) => provider.id !== activatedProviderId);
    return (
      <section className="onboarding-card onboarding-card--success onboarding-card--wide" aria-label="启动引导">
        <p className="onboarding-kicker">启动完成</p>
        <h2>三位角色都到齐了</h2>
        <ul className="onboarding-checks">
          <li>张楚岚 · 通透机敏 · {providerLabel}</li>
          <li>冯宝宝 · 坦率好奇 · {providerLabel}</li>
          <li>王也 · 松弛笃定 · {providerLabel}</li>
        </ul>

        {expansionProviders.length ? (
          <div className="onboarding-expansion">
            <div className="onboarding-expansion-heading">
              <strong>可选扩展</strong>
              <span>验证后可以在团队配置里分配给不同角色</span>
            </div>
            <div className="onboarding-provider-list">
              {expansionProviders.map((provider) => (
                <ProviderSetupCard
                  key={provider.id}
                  provider={provider}
                  busy={props.actionBusy}
                  copiedCommand={copiedCommand}
                  onCopy={copyCommand}
                  onAction={props.onVerifyProvider}
                  actionLabel={`我已登录，验证 ${provider.label}`}
                  compact
                />
              ))}
            </div>
          </div>
        ) : null}

        <div className="onboarding-actions">
          <button type="button" onClick={props.onComplete} disabled={props.actionBusy}>
            开始聊天
          </button>
          <button
            type="button"
            className="button-secondary"
            onClick={props.onOpenTeamSettings ?? props.onComplete}
            disabled={props.actionBusy}
          >
            打开团队配置
          </button>
        </div>
        <p className="onboarding-cost-note" role="note">每次真实 CLI 验证都会使用对应账号发起 1 次模型请求，可能计入套餐额度或产生费用；安装、登录和页面检测本身不会调用模型。</p>
      </section>
    );
  }

  if (props.onboarding?.real_cli_blocked) {
    return (
      <section className="onboarding-card" aria-label="启动引导">
        <p className="onboarding-kicker">离线演示环境</p>
        <h2>这个环境不会调用真实 CLI</h2>
        <p>启动引导需要验证一个真实 CLI。请在开发或日常环境中完成接入。</p>
        <button type="button" className="button-secondary" onClick={props.onSkipRealCli} disabled={props.actionBusy}>
          关闭引导
        </button>
      </section>
    );
  }

  // ready_for_first_chat, first_chat_failed and a failed first verification
  // all return to the same "detect, then verify one real CLI" step.
  const failed = state === "first_chat_failed" || state === "provider_verification_failed";
  const failedProviderId = state === "first_chat_failed"
    ? props.onboarding?.first_chat_provider_id
    : props.onboarding?.verification_provider_id;
  const failure = state === "first_chat_failed"
    ? props.onboarding?.first_chat_error
    : state === "provider_verification_failed"
      ? props.onboarding?.verification_error
      : null;
  const recommendedId = props.onboarding?.recommended_provider_id;
  const ordered = [
    ...providers.filter((provider) => provider.id === recommendedId),
    ...providers.filter((provider) => provider.id !== recommendedId),
  ];
  const anyAvailable = providers.some((provider) => provider.available);
  return (
    <section className={`onboarding-card onboarding-card--wide${failed ? " onboarding-card--error" : ""}`} aria-label="启动引导">
      <p className="onboarding-kicker">{failed ? "首次对话未完成" : "第 1 步，共 2 步"}</p>
      <h2>
        {failed
          ? `${labelOf(failedProviderId)} 没有成功回复`
          : anyAvailable ? "先接入一个真实 CLI" : "还没有检测到可用的 CLI"}
      </h2>
      {anyAvailable ? (
        <p>Unity 的角色通过本机 CLI 回复。确认已经登录后，让张楚岚用它和你打个招呼，这也是对它的第一次真实验证。</p>
      ) : (
        <p>Unity 的角色通过本机 CLI 回复。安装并登录下面任意一个 CLI，然后点击“重新检测”。</p>
      )}
      {failure ? <p role="alert" className="onboarding-error">{failure}</p> : null}

      {ordered.length ? (
        <div className="onboarding-provider-list onboarding-provider-list--primary">
          {ordered.map((provider) => (
            <ProviderSetupCard
              key={provider.id}
              provider={provider}
              recommended={provider.id === recommendedId}
              busy={props.actionBusy}
              copiedCommand={copiedCommand}
              onCopy={copyCommand}
              onAction={props.onStart}
              actionLabel={`我已登录，用 ${provider.label} 打招呼`}
              repeatable
            />
          ))}
        </div>
      ) : (
        <p className="onboarding-error">当前配置中没有可接入的 CLI，请检查 Provider 配置。</p>
      )}

      <p className="onboarding-cost-note" role="note">打招呼会使用所选 CLI 的账号发起 1 次真实模型请求，可能计入套餐额度或产生费用；安装、登录和页面检测本身不会调用模型。</p>
      <div className="onboarding-actions">
        <button type="button" className="button-secondary" onClick={props.onRefreshProviders} disabled={props.actionBusy}>
          重新检测
        </button>
        <button type="button" className="button-secondary" onClick={props.onSkipRealCli} disabled={props.actionBusy}>
          稍后再接入
        </button>
      </div>
      {props.actionError ? <p role="alert" className="onboarding-error">{props.actionError}</p> : null}
    </section>
  );
}

function ProviderSetupCard({
  provider,
  recommended = false,
  compact = false,
  busy,
  copiedCommand,
  onCopy,
  onAction,
  actionLabel,
  repeatable = false,
}: {
  provider: OnboardingProviderStatus;
  recommended?: boolean;
  compact?: boolean;
  busy: boolean;
  copiedCommand: string | null;
  onCopy: (key: string, command?: string | null) => void;
  onAction: (providerId: string) => void;
  actionLabel: string;
  /** Offer the action even for an already verified CLI. */
  repeatable?: boolean;
}) {
  const installKey = `${provider.id}:install`;
  const authKey = `${provider.id}:auth`;
  const status = provider.verified
    ? "已验证，可以分配给角色"
    : provider.available
      ? provider.version || "已安装，等待登录验证"
      : "未安装";

  return (
    <article className={`onboarding-provider-option${recommended ? " is-recommended" : ""}${compact ? " is-compact" : ""}`}>
      <div className="onboarding-provider-option-heading">
        <strong>{provider.label}</strong>
        {recommended ? <span>推荐</span> : null}
      </div>
      <p>{status}</p>
      {!provider.available ? (
        <>
        {!compact && provider.install_command ? (
          <code className="onboarding-install-command">{provider.install_command}</code>
        ) : null}
        {!compact && provider.auth_command ? (
          <p className="onboarding-install-hint">安装后在终端运行 <code>{provider.auth_command}</code> 登录。</p>
        ) : null}
        <div className="onboarding-install-actions">
          {provider.install_command ? (
            <button
              type="button"
              className="button-secondary"
              onClick={() => onCopy(installKey, provider.install_command)}
            >
              {copiedCommand === installKey ? "安装命令已复制" : "复制安装命令"}
            </button>
          ) : null}
          {provider.install_url ? (
            <a href={provider.install_url} target="_blank" rel="noreferrer">官方安装说明</a>
          ) : null}
        </div>
        </>
      ) : !provider.verified || repeatable ? (
        <>
          {provider.auth_command ? (
            <button
              type="button"
              className="onboarding-command"
              onClick={() => onCopy(authKey, provider.auth_command)}
            >
              {copiedCommand === authKey ? "登录命令已复制" : `登录：${provider.auth_command}`}
            </button>
          ) : null}
          <button type="button" onClick={() => onAction(provider.id)} disabled={busy}>
            {busy ? "正在启动…" : actionLabel}
          </button>
        </>
      ) : null}
    </article>
  );
}
