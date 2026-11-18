"""Read-time causal checks for wait_reply; never infer pairing from prose."""
from backend.app.domain.models import MessageRole


ENDED = {"blocked", "completed", "failed", "completed_with_warnings"}


def attach_wait_evidence(events, messages, mission, turns):
    ended = getattr(mission.status, "value", mission.status) in ENDED
    by_turn = {t.turn_id: t for t in turns}
    actual_turns = {e.payload.get("turn_id") for e in events if e.event_type == "turn.started"}
    starts = [e.payload for e in events if e.event_type == "a2a.step.started"]
    cancelled = {s["step_id"] for e in events if e.event_type == "a2a.cancelled"
                 for s in e.payload.get("cancelled_steps", []) if s.get("step_id")}
    published = {m.id: m for m in messages if m.role == MessageRole.AGENT}
    for invoke in events:
        p = invoke.payload
        if invoke.event_type != "a2a.invoke" or p.get("mode") != "wait_reply":
            continue
        owner = by_turn.get(p.get("sender_turn_id"))
        if owner is None:
            continue
        step = p.get("callee_step_id")
        prefix = f"invoke {invoke.id}: "
        if not step:
            owner.a2a_waits.append((None, prefix + "旧事件缺少 step 关联，无法判定"))
            continue
        terminals = [e for e in events if e.event_type in ("a2a.reply", "a2a.reply.failed")
                     and str(e.payload.get("invoke_event_id")) == str(invoke.id)]
        resumes = [s for s in starts if s.get("origin") == "a2a_resume" and s.get("parent_step_id") == step]
        # A cancelled callee closes an invocation before delivery. Cancellation
        # of a resume after delivery excuses only the missing continuation.
        resume_cancelled = any(e.event_type == "a2a.cancelled" and any(
            s.get("origin") == "a2a_resume" and s.get("parent_step_id") == step
            for s in e.payload.get("cancelled_steps", [])) for e in events)
        if step in cancelled or (resume_cancelled and not terminals):
            owner.a2a_waits.append((0 if terminals else 1, prefix + ("取消后仍有回复" if terminals else "cancelled")))
            continue
        if not terminals:
            owner.a2a_waits.append((0 if ended else None,
                                    prefix + "尚无 reply / reply.failed / cancelled"))
            continue
        if len(terminals) != 1:
            owner.a2a_waits.append((0, prefix + "重复终态"))
            continue
        terminal = terminals[0]
        reply = terminal.payload
        expected = [s for s in starts if s.get("step_id") == step and s.get("origin") == "a2a"]
        # Nested waits transfer delivery to the callee's final continuation.
        for deferred in events:
            if deferred.event_type == "a2a.reply.deferred" and str(deferred.payload.get("invoke_event_id")) == str(invoke.id):
                source_turns = {s.get("turn_id") for s in expected}
                if deferred.payload.get("turn_id") not in source_turns:
                    expected = []
                    break
                expected = [s for s in starts if s.get("step_id") == deferred.payload.get("forwarded_to_step_id")]
        error = ""
        if reply.get("turn_id") not in actual_turns or not any(s.get("turn_id") == reply.get("turn_id") and s.get("agent_id") == p.get("target_agent_id") for s in expected):
            error = "回复回合不属于本次求助"
        elif terminal.event_type == "a2a.reply":
            msg = published.get(reply.get("reply_message_id"))
            if not msg or msg.turn_id != reply.get("turn_id") or msg.agent_id != p.get("target_agent_id"):
                error = "reply_message_id 未指向被叫回合的真实消息"
        if not resumes and not resume_cancelled:
            if not ended and not error:
                owner.a2a_waits.append((None, prefix + "已回复，等待恢复回合"))
                continue
            error = error or "缺少发起者恢复回合"
        if len(resumes) > 1:
            error = error or "重复恢复回合"
        if any(s.get("turn_id") not in actual_turns or s.get("agent_id") != p.get("sender_agent_id") for s in resumes):
            error = error or "恢复回合不是发起者"
        owner.a2a_waits.append((0 if error else 1, prefix + (error or terminal.event_type)))
        if terminal.event_type == "a2a.reply.failed":
            for resume in resumes:
                resumed = by_turn.get(resume.get("turn_id"))
                if resumed:
                    resumed.a2a_failed_targets.append(p.get("target_agent_id", ""))
