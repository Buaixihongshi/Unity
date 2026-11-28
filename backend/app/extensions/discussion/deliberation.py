"""Event-backed six-phase discussion policy. Model calls never hold DB locks."""
import hashlib
import json
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from backend.app.adapters.deliberation_errors import MESSAGES, failure_code
from backend.app.build_info import BUILD_ID, digest

from backend.app.extensions.discussion.models import RESULT_TYPES
from backend.app.domain.models import Message, MessageRole, MissionStatus, utc_now
from backend.app.orchestration.discussion_rules import COMMON_DISCUSSION_RULES
from backend.app.extensions.discussion.roundtable import DiscussionError


PROTOCOL = "deliberation_v2"
RULES_VERSION = "deliberation-v2.3"


def is_v2(mission):
    return getattr(getattr(mission, 'discussion', None), 'protocol', None) == PROTOCOL


def occupies_conversation(mission):
    return mission.status in {MissionStatus.PENDING, MissionStatus.RUNNING, MissionStatus.AWAITING_INPUT} or (
        is_v2(mission) and mission.status == MissionStatus.BLOCKED and mission.completed_at is None
    )



class Deliberation:
    def __init__(self, repository):
        self.repo = repository

    def state(self, mission):
        for event in reversed(self.repo.list_events_after(mission.id, 0)):
            if event.event_type == "discussion.v2.state":
                return event.payload
        return {
            "phase": "independent", "revision": 0, "snapshot_id": None,
            "round_index": 0, "leases": {}, "opinions": [], "contributions": [],
            "issues": {}, "reviews": [], "draft_id": None, "final_id": None,
            "synthesizer_id": None, "awaiting_reason": None, "end_reason": None,
            "turns_used": 0, "attempts_used": 0, "pending_control": None,
            "pending_inputs": [], "decisions": {}, "summary_message_ids": [],
            "exchange_agents": [], "agreement": None,
        }

    @contextmanager
    def transaction(self, mission_id):
        with self.repo._transaction(immediate=True):
            mission = self.repo.get_mission(mission_id)
            if mission is None:
                raise DiscussionError("Mission not found", 404)
            if not is_v2(mission):
                raise DiscussionError("Mission is not a V2 discussion", 422)
            yield mission, self.state(mission)

    def save(self, mission, state, **updates):
        state["revision"] += 1
        self.repo.add_event(mission.id, "discussion.v2.state", state)
        self.repo.update_mission(mission.model_copy(update={"updated_at": utc_now(), **updates}))

    def store(self, mission_id, kind, body):
        result = {"id": f"dr_{uuid4().hex}", "kind": kind, **body}
        with self.repo._transaction() as db:
            db.execute("INSERT INTO discussion_results (id,mission_id,kind,data) VALUES (?,?,?,?)",
                       (result["id"], mission_id, kind, json.dumps(result, ensure_ascii=False)))
        return result

    def get(self, mission_id, result_id):
        with self.repo._transaction() as db:
            row = db.execute("SELECT data FROM discussion_results WHERE id=? AND mission_id=?",
                             (result_id, mission_id)).fetchone()
        if row is None:
            raise DiscussionError("Discussion result not found", 404)
        return json.loads(row["data"])

    def freeze(self, mission, state, artifact_root):
        history = self.repo.read_message_history(conversation_id=mission.conversation_id,
                                                mission_id=None if mission.conversation_id else mission.id)
        messages = [e.message.model_dump(mode="json") for e in history.project()]
        materials = {}
        workspace = Path.cwd().resolve()
        total = 0
        omitted = []
        # Only source/document directories, never live databases, sessions,
        # generated outputs, .git or another participant's working directory.
        for folder in ("backend/app", "frontend/src", "config", "docs"):
            for target in sorted((workspace / folder).rglob('*')):
                if not target.is_file() or target.suffix not in {'.py', '.ts', '.tsx', '.css', '.json', '.md'}:
                    continue
                if not target.resolve().is_relative_to(workspace) or '__pycache__' in target.parts:
                    continue
                path = target.relative_to(workspace).as_posix()
                size = target.stat().st_size
                if size > 500_000 or total + size > 8_000_000:
                    omitted.append(path)
                    continue
                content = target.read_text(encoding='utf-8', errors='replace')
                materials[f'artifact:{mission.id}:{path}'] = {'path': path, 'source_mission_id': mission.id,
                    'content': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()}
                total += size
        root = Path(artifact_root).resolve()
        for old in history.missions:
            for event in self.repo.list_events_after(old.id, 0):
                if event.event_type != "artifact":
                    continue
                path = event.payload.get("path", "")
                target = (root / old.id / path).resolve()
                if not target.is_relative_to(root / old.id) or not target.is_file() or target.stat().st_size > 1_000_000:
                    continue
                key = f"artifact:{old.id}:{path}"
                materials[key] = {"path": path, "source_mission_id": old.id,
                                  "content": target.read_text(encoding="utf-8", errors="replace")}
        body = {"messages": messages, "materials": materials, "omitted_materials": omitted,
                "goal": mission.goal, "context_revision": 1}
        body["material_hash"] = digest(body)
        snapshot = self.store(mission.id, "snapshot", body)
        state["snapshot_id"] = snapshot["id"]
        self.repo.add_event(mission.id, "discussion.snapshot_frozen", {
            "snapshot_id": snapshot["id"], "material_hash": snapshot["material_hash"],
            "protocol": PROTOCOL, "rules_version": RULES_VERSION,
        })

    def visible(self, mission, state, lease=None):
        snapshot = self.get(mission.id, state["snapshot_id"])
        snapshot['materials'] = {key: {k: v for k, v in material.items() if k != 'content'}
                                 for key, material in snapshot['materials'].items()}
        ids = []
        phase = lease["phase"] if lease else state["phase"]
        if phase != "independent":
            ids += state["opinions"] + state["contributions"]
        if phase in {"review", "finalizing", "ended"} and state["draft_id"]:
            ids.append(state["draft_id"])
        if phase in {"finalizing", "ended"}:
            ids += state["reviews"]
        return {"snapshot": snapshot, "results": [self.get(mission.id, rid) for rid in ids],
                "issues": state["issues"] if phase != "independent" else {},
                "phase": phase, "round_index": state["round_index"], "draft_id": state["draft_id"] if phase in {"review", "finalizing", "ended"} else None}

    def _lease(self, state, agent_id, turn_id, provider_id=None, token=None):
        lease = state["leases"].get(turn_id)
        if state["phase"] == "ended" or not lease or lease["agent_id"] != agent_id or lease["status"] != "running":
            raise DiscussionError("Discussion turn lease expired")
        if datetime.fromisoformat(lease['expires_at']) <= utc_now():
            raise DiscussionError('Discussion turn lease expired')
        if provider_id is not None and lease["provider_id"] != provider_id:
            raise DiscussionError("Provider attempt expired")
        if not token or not secrets.compare_digest(lease.get("token", ""), token):
            raise DiscussionError("Discussion capability expired")
        return lease

    def renew_turn_lease(self, mission_id, agent_id, turn_id, provider_id, token, seconds):
        """Host-only extension; the phase, provider and capability remain fenced."""
        with self.transaction(mission_id) as (mission, state):
            # A tool submission can commit after the adapter reads "running"
            # but before this transaction. A completed capability needs no renewal.
            existing = state['leases'].get(turn_id)
            if (state['phase'] != 'ended' and existing and existing['status'] == 'submitted'
                    and existing['agent_id'] == agent_id and existing['provider_id'] == provider_id
                    and token and secrets.compare_digest(existing.get('token', ''), token)):
                return False
            lease = self._lease(state, agent_id, turn_id, provider_id, token)
            expiry = max(datetime.fromisoformat(lease['expires_at']),
                         utc_now() + timedelta(seconds=seconds))
            lease['expires_at'] = expiry.isoformat()
            self.save(mission, state)
            return True

    def read_context(self, mission_id, agent_id, turn_id, provider_id, token, *, record_access=True,
                     material_offset=0, history_offset=0, issue_offset=0):
        with self.transaction(mission_id) as (mission, state):
            lease = self._lease(state, agent_id, turn_id, provider_id, token)
            if any(not isinstance(offset, int) or offset < 0 for offset in (material_offset, history_offset, issue_offset)):
                raise DiscussionError('Context offsets must be nonnegative integers', 422)
            full = self.visible(mission, state, lease)
            view = json.loads(json.dumps(full))
            snapshot = view['snapshot']
            messages = snapshot['messages']
            recent = list(reversed(messages))[history_offset:history_offset + 20]
            snapshot['history_index'] = [{k: m.get(k) for k in ('id', 'role', 'agent_id', 'created_at')} |
                {'preview': m['content'][:80], 'total_chars': len(m['content'])} for m in recent]
            snapshot['history_count'] = len(messages)
            snapshot['next_history_offset'] = history_offset + 20 if history_offset + 20 < len(messages) else None
            snapshot['messages'] = [dict(m, content=m['content'][:500], truncated=len(m['content']) > 500)
                                    for m in messages[-4:]]
            materials = list(snapshot['materials'].items())
            snapshot['materials'] = dict(materials[material_offset:material_offset + 20])
            snapshot['material_count'] = len(materials)
            snapshot['next_material_offset'] = material_offset + 20 if material_offset + 20 < len(materials) else None
            exposed, complete, budget = [], set(), 4000
            for result in view['results']:
                length = len(json.dumps(result['data'], ensure_ascii=False))
                if length <= 2000 and length <= budget:
                    exposed.append(result)
                    complete.add(result['id'])
                    budget -= length
                else:
                    metadata = {k: v for k, v in result.items() if k not in {'data', 'submission_hash'}}
                    data = result['data']
                    preview = {key: data[key][:160] for key in ('position_summary',) if key in data}
                    preview['claims'] = [{'id': c['id'], 'decision_key': c['decision_key'][:40], 'statement': c['statement'][:40]}
                                         for c in data.get('claims', [])]
                    exposed.append({**metadata, 'data': preview, 'truncated': True, 'total_chars': length})
            view['results'] = exposed
            issues = list(view['issues'].items())
            view['issues'] = {}
            for key, issue in issues[issue_offset:issue_offset + 20]:
                length = len(json.dumps(issue, ensure_ascii=False))
                if length <= 2000 and length <= budget:
                    view['issues'][key] = issue
                    complete.add(key)
                    budget -= length
                else:
                    view['issues'][key] = {k: issue[k] for k in ('id', 'category', 'status')}
                    view['issues'][key].update(problem_preview=issue['problem'][:120], truncated=True)
            view['issue_count'] = len(issues)
            view['next_issue_offset'] = issue_offset + 20 if issue_offset + 20 < len(issues) else None
            required = {r['id'] for r in full['results']} | set(full['issues'])
            if record_access:
                lease['context_read'] = True
                lease['read_result_ids'] = list(set(lease.get('read_result_ids', [])) | complete)
                seen_messages = {m['id'] for m in snapshot['messages'] if not m['truncated']}
                lease['source_reads'] = list(set(lease.get('source_reads', [])) | seen_messages)
                self.save(mission, state)
            view['required_read_ids'] = sorted(required - set(lease.get('read_result_ids', [])))
            view['read_instruction'] = 'truncated 表示预览不完整。用 read_discussion_result(source_id, offset) 逐段读到 next_offset=null；翻页索引使用 read_context 的 material_offset/history_offset/issue_offset。'
            return view

    def read_entry(self, mission_id, agent_id, turn_id, provider_id, token, source_id, offset=0, max_chars=8000):
        if not isinstance(source_id, str) or not source_id:
            raise DiscussionError('source_id must be a nonempty string', 422)
        if not isinstance(offset, int) or offset < 0 or not isinstance(max_chars, int) or not 1 <= max_chars <= 8000:
            raise DiscussionError('Invalid discussion paging range', 422)
        with self.transaction(mission_id) as (mission, state):
            lease = self._lease(state, agent_id, turn_id, provider_id, token)
            view = self.visible(mission, state, lease)
            entries = {r['id']: r for r in view['results']} | view['issues']
            entries.update({m['id']: m for m in self.get(mission_id, state['snapshot_id'])['messages']})
            if source_id not in entries:
                raise DiscussionError('Entry is outside the visible phase snapshot', 422)
            text = json.dumps(entries[source_id], ensure_ascii=False, sort_keys=True)
            if offset > len(text):
                raise DiscussionError('Offset exceeds source length', 422)
            end = min(len(text), offset + max_chars)
            ranges = lease.setdefault('read_ranges', {}).setdefault(source_id, [])
            ranges.append([offset, end])
            covered = 0
            for start, finish in sorted(ranges):
                if start > covered:
                    break
                covered = max(covered, finish)
            if covered >= len(text):
                lease['read_result_ids'] = list(set(lease.get('read_result_ids', [])) | {source_id})
                lease['source_reads'] = list(set(lease.get('source_reads', [])) | {source_id})
            self.save(mission, state)
            return {'source_id': source_id, 'format': 'json', 'text': text[offset:end], 'offset': offset,
                    'total_chars': len(text), 'next_offset': end if end < len(text) else None}

    def read_material(self, mission_id, agent_id, turn_id, provider_id, token, key, offset=0, max_chars=8000):
        if not isinstance(offset, int) or offset < 0 or not isinstance(max_chars, int) or not 1 <= max_chars <= 8000:
            raise DiscussionError('Invalid material paging range', 422)
        with self.transaction(mission_id) as (mission, state):
            lease = self._lease(state, agent_id, turn_id, provider_id, token)
            material = self.get(mission_id, state['snapshot_id'])['materials'].get(key)
            if material is None:
                raise DiscussionError('Artifact is outside the frozen discussion snapshot', 422)
            if key not in lease.setdefault('source_reads', []):
                lease['source_reads'].append(key)
                self.save(mission, state)
            text = material['content']
            if offset > len(text):
                raise DiscussionError('Offset exceeds material length', 422)
            end = min(len(text), offset + max_chars)
            return {**material, 'content': text[offset:end], 'offset': offset, 'total_chars': len(text),
                    'next_offset': end if end < len(text) else None}

    def claim_batch(self, mission_id, artifact_root, lease_seconds=630):
        with self.transaction(mission_id) as (mission, state):
            if mission.status not in {MissionStatus.PENDING, MissionStatus.RUNNING}:
                return []
            if any(l["status"] in {"running", "reserved"} for l in state["leases"].values()):
                return []
            if not state["snapshot_id"]:
                self.freeze(mission, state, artifact_root)
            phase = state["phase"]
            participants = mission.discussion.participant_ids
            if phase == "independent":
                authors = participants
            elif phase == "exchange":
                relevant = set(state.get("exchange_agents") or participants)
                authors = [participant for participant in participants if participant in relevant]
            elif phase in {"synthesis", "finalizing"}:
                authors = [state["synthesizer_id"]]
            elif phase == "review":
                authors = [p for p in participants if p != state["synthesizer_id"]]
            else:
                return []
            existing = [l for l in state["leases"].values() if l["phase"] == phase and l["round_index"] == state["round_index"]]
            # Retried batches reuse completed submissions and logical turn IDs.
            leases = []
            for agent in authors:
                old = next((l for l in existing if l["agent_id"] == agent), None)
                if old and old["status"] == "submitted":
                    continue
                if old:
                    if old["attempt"] >= 2:
                        self._block(mission, state, "attempt_budget_exhausted")
                        return []
                    lease = old
                else:
                    if state["turns_used"] >= mission.discussion.max_turns:
                        self._block(mission, state, "turn_budget_exhausted")
                        return []
                    lease = {"turn_id": f"turn_{uuid4().hex[:16]}", "agent_id": agent,
                             "phase": phase, "round_index": state["round_index"], "attempt": 0,
                             "provider_id": None, "result_id": None}
                    state["leases"][lease["turn_id"]] = lease
                    state["turns_used"] += 1
                lease.update(status="reserved", error=None, owner_pid=os.getpid(),
                             expires_at=(utc_now() + timedelta(seconds=lease_seconds)).isoformat())
                leases.append(dict(lease))
            self.save(mission, state, status=MissionStatus.RUNNING, completed_at=None)
            return leases

    def authorize(self, mission_id, turn_id, provider_id):
        with self.transaction(mission_id) as (mission, state):
            lease = state["leases"][turn_id]
            if mission.status != MissionStatus.RUNNING or lease["status"] not in {"reserved", "failed"}:
                raise DiscussionError("Discussion turn lease expired")
            if lease["attempt"] >= 2 or state["attempts_used"] >= 2 * mission.discussion.max_turns:
                raise DiscussionError("Discussion attempt budget exhausted")
            lease.update(status="running", provider_id=provider_id, attempt=lease["attempt"] + 1,
                         token=secrets.token_urlsafe(32), execution_active=True, source_reads=[],
                         read_result_ids=[], read_ranges={}, context_read=False)
            state["attempts_used"] += 1
            self.save(mission, state)
            return dict(lease)

    def failed_attempt(self, mission_id, turn_id, error, *, failure=None, exit_code=None):
        with self.transaction(mission_id) as (mission, state):
            lease = state["leases"][turn_id]
            if lease["status"] != "submitted" and state["phase"] != "ended":
                lease.update(status="failed", error=str(error)[-1000:], token=None, execution_active=False,
                             failure_code=failure if failure in MESSAGES else failure_code(error, exit_code=exit_code),
                             exit_code=exit_code)
                self.save(mission, state)

    def execution_finished(self, mission_id, turn_id):
        with self.transaction(mission_id) as (mission, state):
            lease = state['leases'][turn_id]
            if lease.get('execution_active'):
                lease['execution_active'] = False
                self.save(mission, state)

    def submit(self, mission_id, agent_id, turn_id, provider_id, token, body):
        try:
            encoded = json.dumps(body, ensure_ascii=False)
            if len(encoded) > 100_000:
                raise DiscussionError("Submission exceeds 100000 characters", 422)
            with self.transaction(mission_id) as (mission, state):
                existing = state["leases"].get(turn_id)
                if (existing and existing["status"] == "submitted" and existing["agent_id"] == agent_id
                        and existing["provider_id"] == provider_id and token
                        and secrets.compare_digest(existing.get("token", ""), token)):
                    stored = self.get(mission_id, existing["result_id"])
                    if stored["submission_hash"] != digest(body):
                        raise DiscussionError("Turn already submitted with different input")
                    return {"id": stored["id"], "status": "submitted", "sealed": stored["phase"] in {"independent", "exchange", "review"}}
                lease = self._lease(state, agent_id, turn_id, provider_id, token)
                if not lease.get('context_read'):
                    raise DiscussionError('Call read_context before submitting the frozen-phase result', 422)
                model = RESULT_TYPES[lease["phase"]].model_validate(body)
                data = model.model_dump(mode="json")
                view = self.visible(mission, state, lease)
                required_reads = {r['id'] for r in view['results']} | set(view['issues'])
                missing_reads = required_reads - set(lease.get('read_result_ids', []))
                if missing_reads:
                    raise DiscussionError('Read complete source entries before submitting: ' + ', '.join(sorted(missing_reads)), 422)
                refs = self._source_ids(view)
                self._validate_refs(data, refs)
                if lease['phase'] == 'independent':
                    read_sources = set(lease.get('source_reads', [])) | {m['id'] for m in view['snapshot']['messages']}
                    if any(c['evidence_status'] == 'verified' and not set(c['source_ids']) <= read_sources for c in data['claims']):
                        raise DiscussionError('Verified claims must cite sources actually read in this attempt', 422)
                if lease['phase'] == 'exchange':
                    claims = {c['id'] for r in view['results'] if r['kind'] == 'opinion' for c in r['data']['claims']}
                    for item in data['issues']:
                        if not set(item['claim_ids']) <= claims:
                            raise DiscussionError('Issues must cite initial claims', 422)
                        if item['issue_id'] and item['issue_id'] not in view['issues']:
                            raise DiscussionError('Unknown issue reference', 422)
                        if item['issue_id'] and set(item['claim_ids']) != set(view['issues'][item['issue_id']]['claim_ids']):
                            raise DiscussionError('An issue cannot change its original claims', 422)
                if lease["phase"] == "review" and data["draft_id"] != state["draft_id"]:
                    raise DiscussionError("Review targets an outdated draft", 422)
                if lease["phase"] in {"synthesis", "finalizing"}:
                    initial_claims = {c["id"] for rid in state["opinions"] for c in self.get(mission_id, rid)["data"]["claims"]}
                    cited = {sid for section in data["sections"] for sid in section["source_ids"]}
                    if not initial_claims <= cited:
                        raise DiscussionError("Draft must cite every initial claim, including minority views", 422)
                    if not set(state['issues']) <= cited:
                        raise DiscussionError('Draft must cite every issue, including unresolved issues', 422)
                    human_issues = {key for key, issue in state['issues'].items() if issue['category'] == 'needs_human'}
                    if not human_issues <= {key for decision in data['decisions'] for key in decision['issue_ids']}:
                        raise DiscussionError('Every value issue requires an explicit human decision', 422)
                    technical_issues = {key for key, issue in state['issues'].items()
                                        if issue['category'] != 'needs_human'}
                    disposed = {item['issue_id'] for item in data['issue_decisions']}
                    if not technical_issues <= disposed:
                        raise DiscussionError('Every technical issue requires an explicit issue decision', 422)
                    if disposed & human_issues:
                        raise DiscussionError('Value issues are decided by the user, not by the synthesizer', 422)
                    if lease["phase"] == "finalizing":
                        self._validate_revision(mission, state, data)
                    elif data["dispositions"]:
                        raise DiscussionError("Initial draft cannot include review dispositions", 422)
                result_id = f"dr_{uuid4().hex}"
                for index, claim in enumerate(data.get("claims", [])):
                    claim["id"] = f"{result_id}:claim:{index}"
                for index, finding in enumerate(data.get("findings", [])):
                    finding["id"] = f"{result_id}:finding:{index}"
                result = self.store(mission_id, type(model).model_fields["kind"].default, {
                    "id": result_id, "agent_id": agent_id, "turn_id": turn_id,
                    "provider_id": provider_id,
                    "phase": lease["phase"], "round_index": lease["round_index"],
                    "snapshot_id": state["snapshot_id"], "data": data, "submission_hash": digest(body),
                })
                lease.update(status="submitted", result_id=result["id"])
                self.repo.add_event(mission_id, "discussion.result_submitted", {
                    "result_id": result["id"], "turn_id": turn_id, "agent_id": agent_id, "phase": lease["phase"],
                })
                self.save(mission, state)
                return {"id": result["id"], "status": "submitted", "sealed": lease["phase"] in {"independent", "exchange", "review"}}
        except (ValidationError, TypeError, ValueError) as exc:
            if isinstance(exc, DiscussionError):
                raise
            raise DiscussionError(f"Invalid discussion submission: {exc}", 422) from exc

    @staticmethod
    def _source_ids(view):
        snapshot = view["snapshot"]
        ids = {snapshot["id"], *snapshot["materials"], *(m["id"] for m in snapshot["messages"])}
        ids.update(view["issues"])
        for result in view["results"]:
            ids.add(result["id"])
            for item in result["data"].get("claims", []) + result["data"].get("findings", []):
                ids.add(item["id"])
        return ids

    def _validate_refs(self, data, allowed):
        if isinstance(data, dict):
            for key, value in data.items():
                if key in {"source_ids", "claim_ids", "issue_ids"} and not set(value) <= allowed:
                    raise DiscussionError("Reference is outside the visible snapshot", 422)
                if key == "issue_id" and value and value not in allowed:
                    raise DiscussionError("Unknown issue reference", 422)
                self._validate_refs(value, allowed)
        elif isinstance(data, list):
            for value in data:
                self._validate_refs(value, allowed)

    def _validate_revision(self, mission, state, data):
        original = self.get(mission.id, state["draft_id"])["data"]
        findings = [f for rid in state["reviews"] for f in self.get(mission.id, rid)["data"]["findings"]]
        dispositions = data["dispositions"]
        if len(dispositions) != len(findings) or {d["finding_id"] for d in dispositions} != {f["id"] for f in findings}:
            raise DiscussionError("Revision must address each review finding exactly once", 422)
        affected = {f["section_id"] for f in findings}
        old = {s["id"]: s for s in original["sections"]}
        if any(section != old[section["id"]] for section in data["sections"] if section["id"] not in affected):
            raise DiscussionError("Revision changed a section without a review finding", 422)
        if data["decisions"] != original["decisions"]:
            raise DiscussionError("Revision cannot introduce or change value decisions", 422)
        original_issues = {item["issue_id"] for item in original.get("issue_decisions", [])}
        revised_issues = {item["issue_id"] for item in data["issue_decisions"]}
        if original_issues != revised_issues:
            raise DiscussionError("Revision cannot add or remove issue decisions", 422)

    def _publish(self, mission, result, state):
        message = Message(id=f"msg_{uuid4().hex}", mission_id=mission.id,
                          conversation_id=mission.conversation_id, role=MessageRole.AGENT,
                          agent_id=result["agent_id"], turn_id=result["turn_id"],
                          provider_id=result.get('provider_id'),
                          content=render_result(result))
        self.repo.add_message(message)
        if result["kind"] == "draft":
            state["summary_message_ids"].append(message.id)

    def _block(self, mission, state, reason):
        state["error"] = reason
        self.save(mission, state, status=MissionStatus.BLOCKED, completed_at=None)

    def _await(self, mission, state, reason):
        state["awaiting_reason"] = reason
        if reason == "select_synthesizer":
            state["phase"] = "awaiting_synthesizer"
        self.repo.add_event(mission.id, "discussion.awaiting_input", {"reason": reason})
        self.save(mission, state, status=MissionStatus.AWAITING_INPUT, completed_at=None)

    def complete_batch(self, mission_id):
        with self.transaction(mission_id) as (mission, state):
            if state["phase"] == "ended" or mission.status != MissionStatus.RUNNING:
                return
            if state['pending_control'] == 'stop':
                self._stop(mission, state)
                return
            batch = [l for l in state["leases"].values() if l["phase"] == state["phase"] and l["round_index"] == state["round_index"]]
            if not batch or any(l["status"] != "submitted" for l in batch):
                self._block(mission, state, "stage_submission_failed")
                return
            results = [self.get(mission_id, lease["result_id"]) for lease in batch]
            phase = state["phase"]
            for result in results:
                self._publish(mission, result, state)
            self.repo.add_event(mission_id, "discussion.batch_released", {
                "phase": phase, "round_index": state["round_index"], "result_ids": [r["id"] for r in results],
            })
            if phase == "independent":
                state["opinions"] = [r["id"] for r in results]
                agreement = analyze_opinion_agreement(results)
                state["agreement"] = agreement
                user_finish = state["pending_control"] == "finish"
                state["phase"] = "exchange"
                if user_finish or agreement["skip"]:
                    state["exchange_end_reason"] = "exchange_skipped"
                    state["pending_control"] = None
                    state["phase"] = "awaiting_synthesizer"
                    state["exchange_agents"] = []
                    self.repo.add_event(mission_id, "discussion.exchange_skipped", {
                        "reason": "user_finish" if user_finish else agreement["reason"],
                        "shared_decision_keys": agreement["shared_decision_keys"],
                    })
                else:
                    state["exchange_agents"] = agreement["relevant_agent_ids"]
                    state["round_index"] = 1
            elif phase == "exchange":
                state["contributions"] += [r["id"] for r in results]
                self._merge_issues(mission, state, results)
                additions = any(r["data"]["issues"] for r in results)
                open_issues = any(i["status"] in {"open", "unresolved"} for i in state["issues"].values())
                if not additions or not open_issues or state["round_index"] >= mission.discussion.max_exchange_rounds or state["pending_control"] == "finish":
                    state["exchange_end_reason"] = "no_new_information" if not additions else "exchange_budget_exhausted" if state["round_index"] >= mission.discussion.max_exchange_rounds else "issues_classified"
                    for issue in state["issues"].values():
                        if issue["status"] == "open":
                            issue["status"] = "unresolved"
                    state["phase"] = "awaiting_synthesizer"
                    state["pending_control"] = None
                else:
                    state["round_index"] += 1
            elif phase == "synthesis":
                self._apply_issue_decisions(state, results[0]["data"])
                state["draft_id"] = results[0]["id"]
                state["phase"] = "review"
            elif phase == "review":
                state["reviews"] = [r["id"] for r in results]
                state["phase"] = "finalizing"
                if all(r["data"]["no_findings"] for r in results):
                    state["final_id"] = state["draft_id"]
            elif phase == "finalizing":
                self._apply_issue_decisions(state, results[0]["data"])
                state["final_id"] = results[0]["id"]
            if state["pending_inputs"]:
                self._await(mission, state, "revise_input")
            elif state["final_id"]:
                self._finalize(mission, state)
            elif state["phase"] == "awaiting_synthesizer":
                self._await(mission, state, "select_synthesizer")
            else:
                self.save(mission, state)

    def _merge_issues(self, mission, state, results):
        claim_authors = {c["id"]: r["agent_id"] for rid in state["opinions"] for r in [self.get(mission.id, rid)] for c in r["data"]["claims"]}
        votes = {}
        for result in results:
            for item in result["data"]["issues"]:
                if not set(item["claim_ids"]) <= set(claim_authors):
                    raise DiscussionError("Issues must cite initial claims", 422)
                issue_id = item["issue_id"] or "issue_" + digest([sorted(item["claim_ids"]), item["category"]])[:16]
                issue = state["issues"].setdefault(issue_id, {"id": issue_id, "claim_ids": item["claim_ids"],
                    "category": item["category"], "problem": item["problem"], "impact": item["impact"], "positions": {}, "status": "open"})
                issue["positions"][result["agent_id"]] = {**item, "result_id": result["id"]}
                votes.setdefault(issue_id, {})[result["agent_id"]] = item
                if item["category"] in {"needs_human", "needs_information"}:
                    issue["status"] = item["category"]
        for issue_id, items in votes.items():
            issue = state["issues"][issue_id]
            required = {claim_authors[c] for c in issue["claim_ids"]} | set(issue['positions'])
            if (issue['category'] not in {'needs_human', 'needs_information'} and required <= set(items) and all(items[a]["status"] == "resolved" and items[a]["source_ids"] for a in required)
                    and len({items[a]["proposal"] for a in required}) == 1):
                issue["status"] = "resolved"
                issue["resolution"] = next(iter(items.values()))["proposal"]
        # The next round only needs the authors whose claims are still in play.
        relevant = set(state.get("exchange_agents") or [])
        for issue in state["issues"].values():
            if issue["status"] in {"open", "unresolved"}:
                relevant.update(claim_authors[claim_id] for claim_id in issue["claim_ids"] if claim_id in claim_authors)
        state["exchange_agents"] = [participant for participant in mission.discussion.participant_ids if participant in relevant]

    @staticmethod
    def _apply_issue_decisions(state, draft):
        for item in draft.get("issue_decisions", []):
            issue = state["issues"].get(item["issue_id"])
            if issue is None or issue["category"] == "needs_human":
                continue
            issue["technical_decision"] = item["decision"]
            issue["decision_status"] = item["status"]
            if item["status"] == "resolved":
                issue["status"] = "resolved"
                issue["resolution"] = item["decision"]
                issue.pop("deferred", None)
            elif item["status"] == "deferred":
                issue["status"] = "unresolved"
                issue["deferred"] = True
            else:
                issue["status"] = "unresolved"

    def _finalize(self, mission, state):
        final = self.get(mission.id, state["final_id"])
        pending = [d for d in final["data"]["decisions"] if d["id"] not in state["decisions"]]
        if pending:
            self._await(mission, state, "resolve_value_choice")
            return
        warnings = state["final_id"] != state["draft_id"] or any(i["status"] != "resolved" for i in state["issues"].values())
        warnings |= any(c["evidence_status"] != "verified" for rid in state["opinions"] for c in self.get(mission.id, rid)["data"]["claims"])
        state.update(phase="ended", awaiting_reason=None, end_reason="completed_with_reservations" if warnings else "completed")
        self.repo.add_event(mission.id, "discussion.finalized", {"final_id": state["final_id"], "reviewed_draft_id": state["draft_id"], "revision_independently_reviewed": False if state["final_id"] != state["draft_id"] else True})
        self.save(mission, state, status=MissionStatus.COMPLETED_WITH_WARNINGS if warnings else MissionStatus.COMPLETED, completed_at=utc_now())

    def control(self, mission_id, payload, settings=None):
        from backend.app.api.mission_requests import MissionRequest, create_requested_mission
        fingerprint = payload.model_dump(mode="json", exclude={"request_id"})
        with self.transaction(mission_id) as (mission, state):
            for event in self.repo.list_events_after(mission_id, 0):
                if event.event_type == "discussion.v2.control" and event.payload["request_id"] == payload.request_id:
                    if event.payload["request"] != fingerprint:
                        raise DiscussionError("request_id was already used with different input")
                    return event.payload.get("mission_id", mission_id), False
            if payload.expected_revision is None or payload.expected_revision != state["revision"]:
                raise DiscussionError("Discussion revision changed; refresh and retry")
            if state["phase"] == "ended" and payload.action != 'save_knowledge_candidate':
                raise DiscussionError("Discussion has ended; continue in a new mission")
            action = payload.action
            new_mission_id = mission_id
            schedule = False
            if state['pending_control'] == 'stop' and action != 'stop':
                raise DiscussionError('Discussion is stopping')
            if payload.summary_agent_id is not None:
                raise DiscussionError("V2 requires explicit select_synthesizer", 422)
            if action == "stop":
                if any(l['status'] in {'running', 'reserved'} or l.get('execution_active') for l in state['leases'].values()):
                    state['pending_control'] = 'stop'
                    for lease in state['leases'].values():
                        if lease['status'] in {'running', 'reserved'}:
                            lease.update(status='cancelling', token=None)
                    self.save(mission, state, status=MissionStatus.RUNNING)
                else:
                    self._stop(mission, state)
            elif action == "finish":
                if state["phase"] not in {"independent", "exchange"} or mission.status not in {MissionStatus.PENDING, MissionStatus.RUNNING}:
                    raise DiscussionError("Finish is only available during independent/exchange")
                state["pending_control"] = "finish"
                self.save(mission, state)
            elif action == "select_synthesizer":
                if mission.status != MissionStatus.AWAITING_INPUT or state["awaiting_reason"] != "select_synthesizer":
                    raise DiscussionError("Discussion is not awaiting a synthesizer")
                if payload.agent_id not in mission.discussion.participant_ids:
                    raise DiscussionError("Synthesizer must be a discussion participant", 422)
                state.update(synthesizer_id=payload.agent_id, phase="synthesis", awaiting_reason=None)
                self.repo.add_event(mission_id, "discussion.synthesizer_selected", {"agent_id": payload.agent_id})
                self.save(mission, state, status=MissionStatus.PENDING)
                schedule = True
            elif action == "retry":
                if mission.status != MissionStatus.BLOCKED:
                    raise DiscussionError("Discussion is not blocked")
                lease = state["leases"].get(payload.turn_id)
                if not lease or lease["status"] not in {"failed", "interrupted"} or lease["attempt"] >= 2:
                    raise DiscussionError("Turn cannot be retried")
                # A batch resumes only when all failed turns have retry capacity.
                failed = [l for l in state["leases"].values() if l["status"] in {"failed", "interrupted"}]
                if any(l["attempt"] >= 2 for l in failed):
                    raise DiscussionError("Attempt budget exhausted; stop and start a new discussion")
                state["error"] = None
                self.save(mission, state, status=MissionStatus.PENDING)
                schedule = True
            elif action == "record_decision":
                if state["awaiting_reason"] != "resolve_value_choice":
                    raise DiscussionError("Discussion is not awaiting a value decision")
                draft = self.get(mission_id, state["final_id"])["data"]
                decision = next((d for d in draft["decisions"] if d["id"] == payload.decision_id), None)
                if not decision or payload.option_id not in {"defer", *(o["id"] for o in decision["options"])}:
                    raise DiscussionError("Invalid value decision option", 422)
                if payload.decision_id in state["decisions"]:
                    raise DiscussionError("Decision already recorded")
                state["decisions"][payload.decision_id] = payload.option_id
                for issue_id in decision['issue_ids']:
                    issue = state['issues'][issue_id]
                    issue['human_decision'] = {'decision_id': payload.decision_id, 'option_id': payload.option_id}
                    if payload.option_id != 'defer':
                        issue['status'] = 'resolved'
                self.repo.add_event(mission_id, "discussion.decision_recorded", {"decision_id": payload.decision_id, "option_id": payload.option_id})
                choice = ('暂不选择，待以后决定' if payload.option_id == 'defer'
                          else '选择「' + next(o['label'] for o in decision['options'] if o['id'] == payload.option_id) + '」')
                self.repo.add_message(Message(id=f"msg_{uuid4().hex}", mission_id=mission_id,
                    conversation_id=mission.conversation_id, role=MessageRole.USER,
                    content=f"关于「{decision['question']}」，{choice}。"))
                self._finalize(mission, state)
            elif action == 'save_knowledge_candidate':
                if state['phase'] != 'ended' or not state['final_id']:
                    raise DiscussionError('Knowledge candidates require a final result')
                if not state.get('knowledge_candidate_id'):
                    candidate = self.store(mission_id, 'knowledge_candidate', {
                        'goal': mission.goal, 'final_id': state['final_id'], 'snapshot_id': state['snapshot_id'],
                        'source_result_ids': state['opinions'] + state['contributions'] + state['reviews'],
                        'user_decisions': state['decisions'], 'issues': state['issues'],
                        'created_at': utc_now().isoformat(), 'status': 'candidate_not_approved',
                    })
                    state['knowledge_candidate_id'] = candidate['id']
                    self.repo.add_event(mission_id, 'discussion.knowledge_candidate_saved', {'candidate_id': candidate['id']})
                    self.save(mission, state)
            elif action in {"defer_input", "restart_with_input"}:
                if state["awaiting_reason"] != "revise_input":
                    raise DiscussionError("Discussion is not awaiting input revision")
                if set(payload.input_ids or []) != set(state["pending_inputs"]):
                    raise DiscussionError("Pending inputs changed; refresh and retry")
                if action == "defer_input":
                    state.update(pending_inputs=[], awaiting_reason=None)
                    if state["final_id"]:
                        self._finalize(mission, state)
                    elif state["phase"] == "awaiting_synthesizer":
                        self._await(mission, state, "select_synthesizer")
                    else:
                        self.save(mission, state, status=MissionStatus.PENDING)
                        schedule = True
                else:
                    if not payload.goal or not payload.goal.strip() or settings is None:
                        raise DiscussionError("Restart requires a new goal", 422)
                    state.update(phase="ended", awaiting_reason=None, end_reason="input_revised")
                    self.save(mission, state, status=MissionStatus.COMPLETED, completed_at=utc_now())
                    request = MissionRequest(goal=payload.goal, skill=mission.skill, room_mode="roundtable", discussion={
                        "protocol": PROTOCOL, "participant_ids": mission.discussion.participant_ids,
                        "max_exchange_rounds": mission.discussion.max_exchange_rounds,
                    }, request_id=f"followup:{digest(payload.request_id)[:32]}")
                    new, _, _ = create_requested_mission(self.repo, settings, request, mission.conversation_id)
                    new_mission_id, schedule = new.id, True
                    self.repo.add_event(new.id, "discussion.continues", {"source_mission_id": mission_id})
            else:
                raise DiscussionError("Unknown discussion control", 422)
            self.repo.add_event(mission_id, "discussion.v2.control", {"request_id": payload.request_id,
                                "request": fingerprint, "mission_id": new_mission_id})
            return new_mission_id, schedule

    def _stop(self, mission, state):
        state.update(phase='ended', awaiting_reason=None, pending_control=None, end_reason='user_stop')
        for lease in state['leases'].values():
            lease['execution_active'] = False
            if lease['status'] != 'submitted':
                lease.update(status='cancelled', token=None)
        self.save(mission, state, status=MissionStatus.COMPLETED, completed_at=utc_now())

    def interrupt(self, mission_id, content, target, request_id, attachment_ids=None):
        if not request_id:
            raise DiscussionError("Roundtable interruptions require request_id", 422)
        fingerprint = {"content": content, "target_agent_id": target}
        if attachment_ids:
            fingerprint["attachment_ids"] = attachment_ids
        if not content.strip() and not attachment_ids:
            raise DiscussionError("请输入文字或添加图片", 422)
        with self.transaction(mission_id) as (mission, state):
            for event in self.repo.list_events_after(mission_id, 0):
                if event.event_type == "interrupt" and event.payload.get("request_id") == request_id:
                    if event.payload.get("request") != fingerprint:
                        raise DiscussionError("request_id was already used with different input")
                    return
            if state["phase"] == "ended":
                raise DiscussionError("Discussion has ended; continue in a new mission")
            if target and target not in mission.discussion.participant_ids:
                raise DiscussionError("Target must be a discussion participant", 422)
            msg = self.repo.add_interruption(mission_id, content, target, request_id=request_id, request=fingerprint, attachment_ids=attachment_ids)
            state["pending_inputs"].append(msg.id)
            if mission.status == MissionStatus.AWAITING_INPUT:
                self._await(mission, state, "revise_input")
            else:
                self.save(mission, state)

    def recover(self, mission):
        with self.transaction(mission.id) as (mission, state):
            if mission.status == MissionStatus.AWAITING_INPUT or state["phase"] == "ended":
                return
            if mission.status == MissionStatus.RUNNING:
                active = [l for l in state['leases'].values() if l['status'] in {'running', 'reserved'}]
                for lease in active:
                    pid = lease.get('owner_pid')
                    if pid and pid != os.getpid() and datetime.fromisoformat(lease['expires_at']) > utc_now():
                        try:
                            os.kill(pid, 0)
                            return  # Another live server worker owns the lease.
                        except ProcessLookupError:
                            pass
                if state['pending_control'] == 'stop':
                    self._stop(mission, state)
                    return
                if not any(l['status'] in {'running', 'reserved'} for l in state['leases'].values()):
                    for lease in state['leases'].values():
                        lease['execution_active'] = False
                    self.save(mission, state, status=MissionStatus.PENDING)
                    return
                for lease in state["leases"].values():
                    if lease["status"] in {"running", "reserved"}:
                        lease.update(status="interrupted", token=None, error="service_restart")
                self._block(mission, state, "service_restart")

    def user_view(self, mission):
        state = self.state(mission)
        progress = [{k: l.get(k) for k in ("turn_id", "agent_id", "provider_id", "phase", "status", "attempt", "error", "failure_code", "exit_code")} for l in state["leases"].values()]
        for item in progress:
            if item['error']:
                code = item['failure_code'] if item['failure_code'] in MESSAGES else failure_code(item['error'])
                item['failure_code'] = code
                item['error'] = MESSAGES[code]
        released = state["opinions"] + state["contributions"] + state["reviews"]
        released += [rid for rid in (state["draft_id"], state["final_id"]) if rid]
        results = [self.get(mission.id, rid) for rid in dict.fromkeys(released)]
        decisions = self.get(mission.id, state["final_id"])["data"]["decisions"] if state["final_id"] else []
        failed = [l for l in progress if l["status"] in {"failed", "interrupted"}]
        return {"protocol": PROTOCOL, "participant_ids": mission.discussion.participant_ids,
                "phase": state["phase"], "revision": state["revision"], "awaiting_reason": state["awaiting_reason"],
                "turns_used": state["turns_used"], "turns_limit": mission.discussion.max_turns,
                "attempts_used": state["attempts_used"], "round_index": state["round_index"],
                "max_exchange_rounds": mission.discussion.max_exchange_rounds,
                "exchange_end_reason": state.get("exchange_end_reason"),
                "exchange_agents": state.get("exchange_agents", []),
                "agreement": state.get("agreement"),
                "current_speakers": [l["agent_id"] for l in progress if l["status"] in {"reserved", "running"}],
                "current_speaker": None, "pending_control": state["pending_control"],
                "end_reason": state["end_reason"], "error": state.get("error"), "progress": progress,
                "retryable": bool(failed) and all(l["attempt"] < 2 for l in failed),
                "issues": list(state["issues"].values()), "synthesizer_id": state["synthesizer_id"],
                "results": results, "draft_id": state["draft_id"], "final_id": state["final_id"],
                'knowledge_candidate_id': state.get('knowledge_candidate_id'),
                'outcome': {'final_id': state['final_id'], 'reviewed_draft_id': state['draft_id'],
                    'reviewer_ids': [r['agent_id'] for r in results if r['kind'] == 'review'],
                    'unresolved_issue_ids': [key for key, value in state['issues'].items() if value['status'] != 'resolved'],
                    'revision_independently_reviewed': state['final_id'] == state['draft_id'] if state['final_id'] else None,
                    'user_decisions': state['decisions']},
                "value_decisions": decisions, "decisions": state["decisions"], "pending_inputs": state["pending_inputs"],
                "summary_message_ids": state["summary_message_ids"], "unanswered_interrupt_ids": state["pending_inputs"],
                "rules_version": RULES_VERSION, "rules_hash": digest(COMMON_DISCUSSION_RULES), 'build_id': BUILD_ID}


_NORMALIZE_SPACE = re.compile(r"[\s\u3000]+")
_TRAILING_PUNCTUATION = "。．.；;，,、！!？?：:（）()【】[]“”‘’\"'"


def _decision_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _NORMALIZE_SPACE.sub("", text).strip(_TRAILING_PUNCTUATION)


def analyze_opinion_agreement(results):
    """Decide whether Phase 2 can be skipped without treating wording as conflict.

    Only decision-level signals determine agreement: the same decision keys,
    recommendation and applicability conditions, plus no two participants
    explicitly declaring different assumptions or actions. Statements, source
    order, evidence status and unknown lists are recorded for synthesis but do
    not themselves create a conflict.
    """
    positions = {}
    assumptions = {}
    actions = {}
    ambiguous_keys = set()
    for result in results:
        agent_id = result["agent_id"]
        data = result["data"]
        assumptions[agent_id] = sorted({_decision_text(item) for item in data.get("assumptions", [])
                                        if _decision_text(item)})
        actions[agent_id] = sorted({_decision_text(item) for item in data.get("actions", [])
                                    if _decision_text(item)})
        per_agent = {}
        for claim in data["claims"]:
            decision_key = _decision_text(claim["decision_key"]) or claim["decision_key"]
            item = {
                "claim_id": claim["id"], "agent_id": agent_id,
                "recommendation": _decision_text(claim["recommendation"]),
                "conditions": tuple(sorted({_decision_text(value) for value in claim.get("conditions", [])
                                            if _decision_text(value)})),
            }
            positions.setdefault(decision_key, []).append(item)
            per_agent.setdefault(decision_key, []).append(item)
        for decision_key, items in per_agent.items():
            if len({(item["recommendation"], item["conditions"]) for item in items}) > 1:
                ambiguous_keys.add(decision_key)

    shared_keys = sorted(key for key, items in positions.items()
                         if len({item["agent_id"] for item in items}) >= 2)
    conflicts = []
    for decision_key in shared_keys:
        items = positions[decision_key]
        recommendations = {item["recommendation"] for item in items}
        conditions = {item["conditions"] for item in items}
        if decision_key in ambiguous_keys or len(recommendations) > 1 or len(conditions) > 1:
            conflicts.append({
                "decision_key": decision_key,
                "claim_ids": [item["claim_id"] for item in items],
                "recommendations": sorted(recommendations),
                "conditions": [list(value) for value in sorted(conditions)],
            })

    # Only explicit disagreement between two participants is a candidate
    # conflict. One side omitting assumptions or actions is not a conflict.
    assumptions_equal = len({tuple(value) for value in assumptions.values() if value}) <= 1
    actions_equal = len({tuple(value) for value in actions.values() if value}) <= 1
    if conflicts:
        reason = "conflicting_decisions"
    elif not shared_keys:
        reason = "no_shared_decision"
    elif not assumptions_equal:
        reason = "assumption_mismatch"
    elif not actions_equal:
        reason = "action_mismatch"
    else:
        reason = "decisions_agree"

    if not assumptions_equal or not actions_equal or not shared_keys:
        relevant = [result["agent_id"] for result in results]
    else:
        conflict_agents = {item["agent_id"] for conflict in conflicts
                           for item in positions[conflict["decision_key"]]}
        relevant = [result["agent_id"] for result in results if result["agent_id"] in conflict_agents]
    return {
        "skip": reason == "decisions_agree",
        "reason": reason,
        "shared_decision_keys": shared_keys,
        "unshared_decision_keys": sorted(set(positions) - set(shared_keys)),
        "conflicts": conflicts,
        "assumptions_equal": assumptions_equal,
        "actions_equal": actions_equal,
        "relevant_agent_ids": relevant if reason != "decisions_agree" else [],
    }


def render_result(result):
    data = result["data"]
    if result["kind"] == "opinion":
        return data["position_summary"] + "\n\n" + "\n".join(f"- {c['statement']}（{c['evidence_status']}）" for c in data["claims"])
    if result["kind"] == "contribution":
        return "暂无补充。" if data["no_addition"] else "\n\n".join(f"{i['problem']}\n影响：{i['impact']}\n建议或验证：{i['proposal']}" for i in data["issues"])
    if result["kind"] == "review":
        return "本次复核未发现遗漏或误读；不代表结论已经验证。" if data["no_findings"] else "\n\n".join(f"{f['problem']}\n建议修正：{f['desired_change']}" for f in data["findings"])
    labels = {"recommendation": "建议结论", "consensus": "共识与验证状态", "disagreements": "保留的分歧", "pending": "待决事项", "actions": "建议行动", "sources": "来源"}
    sections = "\n\n".join(f"{labels[s['id']]}\n{s['content']}" for s in data["sections"])
    if data.get("issue_decisions"):
        decisions = {"resolved": "已决定", "unresolved": "保留未解", "deferred": "暂缓决定"}
        sections += "\n\n问题处置\n" + "\n".join(
            f"- {decisions[item['status']]}：{item['decision']}" for item in data["issue_decisions"])
    return sections
