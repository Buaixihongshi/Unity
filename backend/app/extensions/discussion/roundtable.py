"""Event-backed roundtable policy shared by the runner and both MCP transports.

Every admission, turn lease and terminal transition holds BEGIN IMMEDIATE. No
in-memory queue or subprocess-specific budget is authoritative for roundtables.
"""
from contextlib import contextmanager
from dataclasses import asdict
from uuid import uuid4

from backend.app.domain.errors import DiscussionError  # noqa: F401  (re-exported)
from backend.app.domain.models import MissionStatus, RoomMode, TurnStatus, utc_now
from backend.app.orchestration.speaker import WorkflowStep


ACTIVE = {MissionStatus.PENDING, MissionStatus.RUNNING}


def initial_state(mission):
    return {
        'phase': 'opening', 'turns_used': 0, 'current': None,
        'opening_remaining': list(mission.discussion.participant_ids),
        'next_index': 0, 'last_success': None, 'queue': [], 'intents': {},
        'pending_control': None, 'summary_agent_id': None, 'closing_reason': None,
        'pending_interrupts': [], 'deferred_interrupts': [], 'unanswered_interrupt_ids': [],
        'summary_message_ids': [], 'end_reason': None,
    }


def state_from_events(mission, events):
    for event in reversed(events):
        if event.event_type == 'discussion.state':
            return event.payload
    return initial_state(mission)


def discussion_view(mission, events):
    if mission.room_mode != RoomMode.ROUNDTABLE or mission.discussion is None:
        return None
    if mission.discussion.protocol == 'deliberation_v2':
        return None  # Repository-backed V2 projection is supplied by mission_detail.
    state = state_from_events(mission, events)
    current = state['current']
    return {
        'participant_ids': mission.discussion.participant_ids,
        'phase': state['phase'], 'turns_used': state['turns_used'],
        'turns_limit': mission.discussion.max_turns,
        'current_speaker': current['agent_id'] if current else None,
        'current_turn_id': current['turn_id'] if current else None,
        'pending_control': state['pending_control'], 'end_reason': state['end_reason'],
        'summary_message_ids': state['summary_message_ids'],
        'unanswered_interrupt_ids': state['unanswered_interrupt_ids'],
    }


class Roundtable:
    def __init__(self, repository):
        self.repo = repository

    @contextmanager
    def transaction(self, mission_id):
        with self.repo._transaction(immediate=True):
            mission = self.repo.get_mission(mission_id)
            if mission is None:
                raise DiscussionError('Mission not found', 404)
            if mission.room_mode != RoomMode.ROUNDTABLE or mission.discussion is None:
                raise DiscussionError('Mission is not a roundtable', 422)
            events = self.repo.list_events_after(mission_id, 0)
            yield mission, state_from_events(mission, events), events

    def save(self, mission, state):
        self.repo.add_event(mission.id, 'discussion.state', state)

    def active(self, mission, state):
        if mission.status not in ACTIVE or state['phase'] == 'ended':
            raise DiscussionError('Discussion has ended; continue in a new mission')
        if mission.conversation_id:
            conv = self.repo.get_conversation(mission.conversation_id)
            if conv and conv.status == 'archived':
                raise DiscussionError('Conversation is archived')

    @staticmethod
    def replay(events, event_type, request_id, fingerprint):
        for event in events:
            if event.event_type == event_type and event.payload.get('request_id') == request_id:
                if event.payload.get('request') != fingerprint:
                    raise DiscussionError('request_id was already used with different input')
                return event
        return None

    def interrupt(self, mission_id, content, target, request_id, attachment_ids=None):
        if not request_id:
            raise DiscussionError('Roundtable interruptions require request_id', 422)
        fingerprint = {'content': content, 'target_agent_id': target}
        if attachment_ids:
            fingerprint["attachment_ids"] = attachment_ids
        if not content.strip() and not attachment_ids:
            raise DiscussionError("请输入文字或添加图片", 422)
        with self.transaction(mission_id) as (mission, state, events):
            if self.replay(events, 'interrupt', request_id, fingerprint):
                return
            self.active(mission, state)
            if state['phase'] == 'closing' or state['pending_control']:
                raise DiscussionError('Discussion is closing or stopping; save this input for the next discussion')
            if target is not None and target not in mission.discussion.participant_ids:
                raise DiscussionError('Target must be a discussion participant', 422)
            message = self.repo.add_interruption(mission_id, content, target, request_id=request_id, request=fingerprint, attachment_ids=attachment_ids)
            event = self.repo.list_events_after(mission_id, 0)[-1]
            item = {'event_id': event.id, 'message_id': message.id, **fingerprint}
            state['pending_interrupts'].append(item)
            self.save(mission, state)

    def control(self, mission_id, action, request_id, summary_agent_id=None):
        fingerprint = {'action': action, 'summary_agent_id': summary_agent_id}
        with self.transaction(mission_id) as (mission, state, events):
            if self.replay(events, 'discussion.control_requested', request_id, fingerprint):
                return
            self.active(mission, state)
            if summary_agent_id and (action != 'finish' or summary_agent_id not in mission.discussion.participant_ids):
                raise DiscussionError('Invalid summary participant', 422)
            if action not in ('finish', 'stop'):
                raise DiscussionError('Unknown discussion control', 422)
            if state['phase'] == 'closing' and action == 'finish':
                raise DiscussionError('Already closing')
            self.repo.add_event(mission_id, 'discussion.control_requested', {
                'request_id': request_id, 'request': fingerprint, **fingerprint,
            })
            if state['pending_control'] != 'stop':
                state['pending_control'] = action
                state['summary_agent_id'] = summary_agent_id
            self.save(mission, state)

    def cancel_queue(self, mission, state, reason):
        if state['queue'] or state['intents']:
            self.repo.add_event(mission.id, 'a2a.cancelled', {
                'reason': reason, 'pending_count': len(state['queue']),
                'cancelled_steps': state['queue'], 'scheduled_intents': state['intents'],
            })
        state['queue'] = []
        state['intents'] = {}

    def end(self, mission, state, reason, *, blocked=False):
        self.cancel_queue(mission, state, reason)
        if state['current'] and reason in ('service_restart', 'runner_failed'):
            current = state['current']
            self.repo.add_event(mission.id, 'turn.blocked', {
                'turn_id': current['turn_id'], 'agent_id': current['agent_id'],
                'provider_id': current['provider_id'], 'attempt': current.get('attempt', 1),
                'duration_ms': 0, 'error': reason,
            })
        pending = list(state['pending_interrupts']) + list(state['deferred_interrupts'])
        if state['current']:
            pending += state['current']['interrupts']
        seen = set()
        for item in pending:
            if item['event_id'] in seen:
                continue
            seen.add(item['event_id'])
            self.repo.add_event(mission.id, 'interrupt.cancelled', {
                'interrupt_event_id': item['event_id'], 'reason': reason,
            })
            if item['event_id'] not in state['unanswered_interrupt_ids']:
                state['unanswered_interrupt_ids'].append(item['event_id'])
        events = self.repo.list_events_after(mission.id, 0)
        handled = {e.payload['request_id'] for e in events if e.event_type == 'discussion.control_handled'}
        for event in events:
            if event.event_type == 'discussion.control_requested' and event.payload['request_id'] not in handled:
                self.repo.add_event(mission.id, 'discussion.control_handled', {
                    'request_id': event.payload['request_id'], 'outcome': reason,
                })
        state.update(phase='ended', current=None, pending_control=None, end_reason=reason,
                     pending_interrupts=[], deferred_interrupts=[])
        mission = mission.model_copy(update={
            'status': MissionStatus.BLOCKED if blocked else MissionStatus.COMPLETED,
            'updated_at': utc_now(), 'completed_at': utc_now(),
        })
        self.repo.update_mission(mission)
        self.repo.add_event(mission.id, 'discussion.ended', {'end_reason': reason, 'turns_used': state['turns_used']})
        self.save(mission, state)
        return mission

    def claim(self, mission_id):
        with self.transaction(mission_id) as (mission, state, events):
            if mission.status not in ACTIVE or state['phase'] == 'ended' or state['current']:
                return None
            self.active(mission, state)
            if state['pending_control'] == 'stop':
                self.end(mission, state, 'user_stop')
                return None
            if mission.status == MissionStatus.PENDING:
                mission = mission.model_copy(update={'status': MissionStatus.RUNNING, 'updated_at': utc_now()})
                self.repo.update_mission(mission)
                self.repo.add_event(mission.id, 'discussion.started', mission.discussion.model_dump())
            participants = mission.discussion.participant_ids
            remaining = mission.discussion.max_turns - state['turns_used']
            closing = remaining <= 1 or state['pending_control'] == 'finish'
            interrupts = state['pending_interrupts']
            state['pending_interrupts'] = []
            if closing:
                if not state['last_success']:
                    state['pending_interrupts'] = interrupts
                    self.end(mission, state, 'user_finish' if state['pending_control'] else 'no_successful_turn')
                    return None
                self.cancel_queue(mission, state, 'closing')
                agent = state['summary_agent_id'] or participants[(participants.index(state['last_success']) + 1) % len(participants)]
                step = WorkflowStep(agent, '收束讨论：保留共识、分歧和待验证问题，说明下一步；回应所有用户补充。', is_closing=True)
                state['closing_reason'] = 'user_finish' if state['pending_control'] == 'finish' else 'budget_exhausted'
                if state['pending_control'] == 'finish':
                    for event in events:
                        if event.event_type == 'discussion.control_requested':
                            self.repo.add_event(mission.id, 'discussion.control_handled', {
                                'request_id': event.payload['request_id'], 'outcome': 'closing_started',
                            })
                    state['pending_control'] = None
                interrupts += state['deferred_interrupts']
                state['deferred_interrupts'] = []
                phase = 'closing'
            else:
                target = next((i['target_agent_id'] for i in reversed(interrupts) if i['target_agent_id']), None)
                if interrupts:
                    self.cancel_queue(mission, state, 'interrupt')
                if target and (target in state['opening_remaining'] or remaining > len(state['opening_remaining']) + 1):
                    step = WorkflowStep(target, '优先回应用户的新条件，并结合已有观点继续讨论。')
                elif state['queue']:
                    step = WorkflowStep(**state['queue'].pop(0))
                else:
                    agent = state['opening_remaining'][0] if state['opening_remaining'] else participants[state['next_index']]
                    step = WorkflowStep(agent, '回应前面具体观点，补充依据或修正判断；无需强行制造分歧。')
                phase = 'opening' if state['opening_remaining'] else 'exchange'
                if step.agent_id in state['opening_remaining']:
                    state['opening_remaining'].remove(step.agent_id)
                state['next_index'] = (participants.index(step.agent_id) + 1) % len(participants)
            intent = state['intents'].pop(step.agent_id, [])
            notes = '\n'.join('邀请目的：' + i['purpose'] for i in intent)
            notes += '\n' + '\n'.join('用户补充（目标={}）：{}'.format(i['target_agent_id'] or '所有人', i['content']) for i in interrupts)
            if state['phase'] != phase:
                self.repo.add_event(mission.id, 'discussion.phase_changed', {'phase': phase})
            turn_id = f'turn_{state["turns_used"] + 1}_{uuid4().hex[:8]}'
            state['phase'] = phase
            state['turns_used'] += 1
            state['current'] = {**asdict(step), 'turn_id': turn_id, 'provider_id': None, 'interrupts': interrupts}
            self.repo.add_event(mission.id, 'discussion.turn_claimed', {
                'turn_id': turn_id, 'step_id': step.step_id, 'agent_id': step.agent_id, 'origin': step.origin,
                'phase': phase, 'turns_used': state['turns_used'],
            })
            self.save(mission, state)
            return mission, state, step, turn_id, notes

    def authorize_attempt(self, mission_id, turn_id, provider_id, attempt=1):
        with self.transaction(mission_id) as (mission, state, events):
            self.active(mission, state)
            if not state['current'] or state['current']['turn_id'] != turn_id:
                raise DiscussionError('Turn lease expired')
            state['current']['provider_id'] = provider_id
            state['current']['attempt'] = attempt
            self.save(mission, state)

    @contextmanager
    def tool_scope(self, mission_id, agent_id, turn_id, provider_id=None):
        with self.transaction(mission_id) as (mission, state, events):
            self.active(mission, state)
            current = state['current']
            if not current or current['turn_id'] != turn_id or current['agent_id'] != agent_id:
                raise DiscussionError('Turn lease expired or caller is not the current speaker')
            if provider_id and current['provider_id'] != provider_id:
                raise DiscussionError('Provider attempt expired')
            yield

    def invoke(self, mission_id, sender, turn_id, target, purpose, mode):
        with self.transaction(mission_id) as (mission, state, events):
            participants = mission.discussion.participant_ids
            if target not in participants or target == sender:
                raise DiscussionError('A2A target must be another discussion participant', 422)
            if mode not in ('fire_and_forget', 'wait_reply') or not isinstance(purpose, str) or not purpose.strip():
                raise DiscussionError('Invalid A2A mode or purpose', 422)
            if state['phase'] == 'closing' or state['pending_control']:
                raise DiscussionError('No invitations accepted while closing or stopping')
            for event in events:
                if event.event_type == 'discussion.invitation' and event.payload['sender_turn_id'] == turn_id:
                    if (event.payload['target_agent_id'], event.payload['purpose'], event.payload['mode']) == (target, purpose, mode):
                        return event.payload['result']
                    if mode == 'wait_reply' and event.payload['mode'] == 'wait_reply':
                        raise DiscussionError('Only one wait_reply is accepted per turn')
            queue = state['queue']
            if mode == 'wait_reply' and target in {q['agent_id'] for q in queue if q['origin'] == 'a2a_resume'}:
                raise DiscussionError('A2A wait cycle')
            free = mission.discussion.max_turns - state['turns_used'] - 1
            # Upcoming baseline speakers, excluding already reserved queue slots.
            planned = list(state['opening_remaining'])
            cursor = state['next_index']
            while len(planned) < max(0, free - len(queue)):
                planned.append(participants[cursor])
                cursor = (cursor + 1) % len(participants)
            scheduled = mode == 'fire_and_forget' and (target in planned or any(q['agent_id'] == target for q in queue))
            data = {'sender_agent_id': sender, 'sender_turn_id': turn_id, 'target_agent_id': target, 'purpose': purpose, 'mode': mode}
            if scheduled:
                state['intents'].setdefault(target, []).append(data)
                self.repo.add_event(mission_id, 'a2a.skipped', {**data, 'reason': 'target already scheduled; purpose retained'})
                result = {'status': 'scheduled', 'queued_position': 0, 'skipped': True, 'reply_pending': False}
            else:
                callee = WorkflowStep(target, purpose, origin='a2a', sender_agent_id=sender, sender_turn_id=turn_id)
                new_steps = [asdict(callee)]
                if mode == 'wait_reply':
                    new_steps.append(asdict(WorkflowStep(sender, '结合受邀者回复继续发言。', origin='a2a_resume', sender_agent_id=sender, sender_turn_id=turn_id, parent_step_id=callee.step_id)))
                covered = {q['agent_id'] for q in queue + new_steps}
                required = len(queue) + len(new_steps) + len(set(state['opening_remaining']) - covered)
                if required > free:
                    raise DiscussionError('Insufficient discussion budget; opening turns, accepted resumes and summary are reserved')
                state['queue'] = new_steps + queue
                self.repo.add_event(mission_id, 'a2a.invoke', {**data, 'step_id': callee.step_id, 'queued_position': 1})
                result = {'status': 'accepted', 'queued_position': 1, 'reply_pending': mode == 'wait_reply', 'reserved_turns': len(new_steps)}
                if mode == 'wait_reply':
                    result['resume_position'] = 2
            result['remaining_turns'] = free
            self.repo.add_event(mission_id, 'discussion.invitation', {**data, 'result': result})
            self.save(mission, state)
            return result

    def complete(self, mission_id, turn_id, outcome):
        with self.transaction(mission_id) as (mission, state, events):
            current = state['current']
            if mission.status not in ACTIVE or not current or current['turn_id'] != turn_id:
                return self.repo.get_mission(mission_id)
            messages = [m for m in self.repo.list_messages(mission_id) if m.turn_id == turn_id and m.role == 'agent']
            if outcome.status != TurnStatus.COMPLETED or not messages:
                return self.end(mission, state, 'summary_failed' if current['is_closing'] else 'provider_failed', blocked=True)
            resume = next((q for q in state['queue'] if q['origin'] == 'a2a_resume' and q['parent_step_id'] == current['step_id']), None)
            if resume:
                invocation = next((e for e in events if e.event_type == 'a2a.invoke' and e.payload.get('step_id') == current['step_id']), None)
                reply_text = '\n'.join(m.content for m in messages)
                self.repo.add_event(mission_id, 'a2a.reply', {
                    'turn_id': turn_id, 'agent_id': current['agent_id'],
                    'invoke_event_id': invocation.id if invocation else None,
                    'sender_agent_id': resume['agent_id'], 'sender_resume_step_id': resume['step_id'],
                    'reply_text': reply_text[:1500], 'reply_message_id': messages[-1].id,
                    'truncated': len(reply_text) > 1500,
                })
            state['last_success'] = current['agent_id']
            for item in current['interrupts']:
                if item['target_agent_id'] and item['target_agent_id'] != current['agent_id'] and not current['is_closing']:
                    state['deferred_interrupts'].append(item)
                    if item['event_id'] not in state['unanswered_interrupt_ids']:
                        state['unanswered_interrupt_ids'].append(item['event_id'])
                    self.repo.add_event(mission_id, 'interrupt.deferred', {'interrupt_event_id': item['event_id'], 'reason': 'target not separately answered; reserved for summary'})
                else:
                    self.repo.add_event(mission_id, 'interrupt.handled', {
                        'interrupt_event_id': item['event_id'], 'target_agent_id': item['target_agent_id'],
                        'handled_by_agent_id': current['agent_id'], 'turn_id': turn_id,
                        'handled_in_summary': current['is_closing'],
                    })
            if current['is_closing']:
                state['summary_message_ids'] = [m.id for m in messages]
            state['current'] = None
            if state['pending_control'] == 'stop':
                return self.end(mission, state, 'user_stop')
            if current['is_closing']:
                return self.end(mission, state, state['closing_reason'])
            self.save(mission, state)
            return mission

    def fail(self, mission_id, reason='runner_failed'):
        from backend.app.extensions.discussion.deliberation import Deliberation, is_v2
        if is_v2(self.repo.get_mission(mission_id)):
            policy = Deliberation(self.repo)
            with policy.transaction(mission_id) as (mission, state):
                if state['pending_control'] == 'stop':
                    policy._stop(mission, state)
                    return self.repo.get_mission(mission_id)
                if state['phase'] != 'ended':
                    for lease in state['leases'].values():
                        lease['execution_active'] = False
                        if lease['status'] in {'running', 'reserved'}:
                            lease.update(status='failed', error=reason, token=None)
                    policy._block(mission, state, reason)
            return self.repo.get_mission(mission_id)
        with self.transaction(mission_id) as (mission, state, events):
            if mission.status in ACTIVE:
                return self.end(mission, state, reason, blocked=True)
            return mission

    def recover(self):
        for mission in self.repo.list_missions():
            from backend.app.extensions.discussion.deliberation import Deliberation, is_v2
            if is_v2(mission):
                Deliberation(self.repo).recover(mission)
                continue
            if mission.room_mode == RoomMode.ROUNDTABLE and mission.status in ACTIVE:
                self.fail(mission.id, 'service_restart')
