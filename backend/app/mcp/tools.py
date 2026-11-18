import hashlib
import os
import stat
from functools import partial, wraps
from inspect import signature

from pathlib import Path
from typing import Callable, Dict, List, Optional, Set
from uuid import uuid4

from backend.app import extension_points
from backend.app.domain.history import select_context_messages
from backend.app.domain.models import (
    Message,
    MessageRole,
    is_provider_verification_mission,
    utc_now,
)
from backend.app.orchestration.events import (
    record_a2a_invoke,
    record_a2a_request,
    record_a2a_skipped,
    record_tool_call,
    record_tool_error,
    record_tool_result,
)
from backend.app.orchestration.speaker import WorkflowStep, resume_purpose
from backend.app.storage.repository import Repository


def _clip_text(value: object, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mission_context_digest(result: dict) -> str:
    """The non-deliberation `read_context` answer: task plus recent messages."""
    mission = result.get("mission") if isinstance(result.get("mission"), dict) else {}
    messages = result.get("messages") if isinstance(result.get("messages"), list) else []
    lines = ["任务：" + _clip_text(mission.get("goal") or mission.get("title"), 140)]
    lines.append("历史消息：{n} 条".format(n=len(messages)))
    if messages and isinstance(messages[-1], dict):
        lines.append("最近：" + _clip_text(messages[-1].get("content"), 200))
    return "\n".join(lines)


class MCPToolError(ValueError):
    pass


# invoke_agent purpose is agent-written text shown to another agent; keep it a
# brief hand-off, not a channel for unbounded prompt payloads.
MAX_A2A_PURPOSE_CHARS = 4000


def extension_guard(fn):
    """Let installed extensions wrap this tool (e.g. roundtable turn scope)."""
    call_signature = signature(fn)

    @wraps(fn)
    def guarded(self, *args, **kwargs):
        arguments = call_signature.bind(self, *args, **kwargs).arguments
        return extension_points.guarded(fn.__name__, self, arguments, lambda: fn(self, *args, **kwargs))
    return guarded


class MCPTools:
    def __init__(
        self,
        repository: Repository,
        artifact_root: Path,
        *,
        a2a_enqueue: Optional[Callable[[WorkflowStep], "int | None"]] = None,
        valid_agent_ids: Optional[Set[str]] = None,
        max_a2a_invocations: int = 0,
        a2a_budget_used: Optional[Callable[[], int]] = None,
        peek_resume_chain: Optional[Callable[[str], List[str]]] = None,
        is_target_scheduled: Optional[Callable[[str], bool]] = None,
        scheduled_agent_ids: Optional[Set[str]] = None,
        receipt_id: Optional[str] = None,
        current_step_origin: Optional[str] = None,
        lease_token: Optional[str] = None,
        workspace_root: Optional[Path] = None,
        memory_root: Optional[Path] = None,
        tasks_enabled: bool = True,
    ):
        self.repository = repository
        self.tasks_enabled = tasks_enabled
        self.artifact_root = artifact_root
        self.memory_root = Path(memory_root) if memory_root else None
        self.a2a_enqueue = a2a_enqueue
        self.valid_agent_ids = set(valid_agent_ids or [])
        self.max_a2a_invocations = max_a2a_invocations
        self.a2a_budget_used = a2a_budget_used or (lambda: 0)
        self._peek_resume_chain = peek_resume_chain
        if is_target_scheduled is None and scheduled_agent_ids is not None:
            scheduled = set(scheduled_agent_ids)
            is_target_scheduled = scheduled.__contains__
        self._is_target_scheduled = is_target_scheduled
        self.receipt_id = receipt_id
        self._current_step_origin = current_step_origin
        self.lease_token = lease_token
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()

    def __getattr__(self, name):
        # Tools contributed by installed extensions, e.g. tools.shared_work(...).
        method = extension_points.tool_methods.get(name)
        if method is None:
            raise AttributeError(name)
        return partial(method, self)

    def scheduled_targets(self) -> List[str]:
        """Agents with a remaining declared turn, for the subprocess MCP server."""
        if self._is_target_scheduled is None:
            return []
        return sorted(a for a in self.valid_agent_ids if self._is_target_scheduled(a))

    @property
    def current_step_origin(self) -> Optional[str]:
        """Workflow origin for the active turn, used by host execution policy."""
        return self._current_step_origin

    def read_context(
        self,
        mission_id: str,
        limit: int = 20,
        *,
        agent_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        material_offset: int = 0,
        history_offset: int = 0,
        issue_offset: int = 0,
        message_ids: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        taken_over = extension_points.first('tool_override', self, 'read_context', {
            'mission_id': mission_id, 'agent_id': agent_id, 'turn_id': turn_id, 'provider_id': provider_id,
            'material_offset': material_offset, 'history_offset': history_offset, 'issue_offset': issue_offset})
        if taken_over is not None:
            return taken_over
        if agent_id and turn_id:
            record_tool_call(
                self.repository,
                mission_id,
                turn_id,
                agent_id,
                "read_context",
                {"limit": limit},
                provider_id,
            )
        mission = self.repository.get_mission(mission_id)
        if mission is None:
            raise MCPToolError(f"Unknown mission: {mission_id}")
        conversation_id = mission.conversation_id if not is_provider_verification_mission(mission) else None
        history = self.repository.read_message_history(
            conversation_id=conversation_id, mission_id=None if conversation_id else mission_id,
        )
        entries = history.project()
        messages = select_context_messages(entries, limit)
        missing_message_ids: List[str] = []
        if message_ids:
            # 原文补读：按消息 ID 取未截断全文，绕过条数窗口。
            wanted = [str(value) for value in message_ids if isinstance(value, str)][:50]
            by_id = {entry.message.id: entry.message for entry in entries}
            missing_message_ids = [value for value in wanted if value not in by_id]
            messages = [by_id[value] for value in wanted if value in by_id]
        result = {
            "mission": mission.model_dump(mode="json"),
            "messages": [message.model_dump(mode="json") for message in messages],
        }
        if missing_message_ids:
            result["missing_message_ids"] = missing_message_ids
        if conversation_id and agent_id and self.memory_root is not None:
            from backend.app.memory.assembly import assemble
            sections = assemble(self.repository, self.memory_root, conversation_id, agent_id)
            result["memory"] = {
                "entries": sections.memory_entries,
                "handovers": sections.handovers,
            }
            if sections.checkpoint:
                result["checkpoint"] = sections.checkpoint
        if conversation_id:
            for extra in extension_points.collect('context_extras', self, mission, conversation_id):
                result.update(extra)
        if agent_id and turn_id:
            record_tool_result(
                self.repository,
                mission_id,
                turn_id,
                agent_id,
                "read_context",
                summary="Returned {n} messages".format(n=len(messages)),
                provider_id=provider_id,
                output=_mission_context_digest(result),
            )
        return result

    def _require_memory_store(self):
        if self.memory_root is None:
            raise MCPToolError("记忆存储未配置，本环境不可用长期记忆工具")
        from backend.app.memory.store import MemoryStore, MemoryError
        return MemoryStore(self.repository, self.memory_root), MemoryError

    @extension_guard
    def memory(self, mission_id, agent_id, turn_id, provider_id, action="read", data=None):
        """Long-term, cross-conversation memory of the calling agent only."""
        from backend.app.orchestration.events import record_tool_call, record_tool_result
        mission = self._require_mission(mission_id)
        if not agent_id:
            raise MCPToolError("memory 需要 agent_id")
        store, MemoryError_ = self._require_memory_store()
        value = data if isinstance(data, dict) else {}
        try:
            record_tool_call(self.repository, mission_id, turn_id, agent_id, 'memory',
                             {'action': action, 'entry_id': value.get('entry_id')}, provider_id)
            if action == 'read':
                entry_id = value.get('entry_id')
                if entry_id:
                    entry = store.read_entry(str(entry_id))
                    if entry['agent_id'] != agent_id:
                        raise MCPToolError("只能读取自己的记忆")
                    record_tool_result(self.repository, mission_id, turn_id, agent_id, 'memory',
                                       summary='read full', provider_id=provider_id,
                                       output=entry['body'][:200])
                    return entry
                entries = store.read_entries(agent_id)
                record_tool_result(self.repository, mission_id, turn_id, agent_id, 'memory',
                                   summary='read {n} entries'.format(n=len(entries)), provider_id=provider_id)
                return {'entries': entries}
            if action == 'write':
                entry = store.write(agent_id, title=value.get('title') or '', body=value.get('body') or '',
                                    tags=value.get('tags') or [], source_refs=value.get('source_refs') or [])
                self.repository.add_event(mission_id, 'memory.written', {
                    'agent_id': agent_id, 'turn_id': turn_id, 'entry_id': entry['id'],
                    'title': entry['title']})
                record_tool_result(self.repository, mission_id, turn_id, agent_id, 'memory',
                                   summary='written', provider_id=provider_id)
                return {'id': entry['id'], 'title': entry['title'], 'revision': entry['revision']}
            if action == 'revise':
                entry_id = str(value.get('entry_id') or '')
                existing = store.read_entry(entry_id)
                if existing['agent_id'] != agent_id:
                    raise MCPToolError("只能修订自己的记忆")
                entry = store.revise(entry_id,
                                     title=value.get('title'),
                                     body=value.get('body'),
                                     tags=value.get('tags') if 'tags' in value else None)
                self.repository.add_event(mission_id, 'memory.revised', {
                    'agent_id': agent_id, 'turn_id': turn_id, 'entry_id': entry_id,
                    'revision': entry['revision']})
                record_tool_result(self.repository, mission_id, turn_id, agent_id, 'memory',
                                   summary='revised r{}'.format(entry['revision']), provider_id=provider_id)
                return {'id': entry['id'], 'revision': entry['revision']}
            if action == 'forget':
                entry_id = str(value.get('entry_id') or '')
                existing = store.read_entry(entry_id)
                if existing['agent_id'] != agent_id:
                    raise MCPToolError("只能忘记自己的记忆")
                entry = store.set_status(entry_id, 'forgotten')
                self.repository.add_event(mission_id, 'memory.forgotten', {
                    'agent_id': agent_id, 'turn_id': turn_id, 'entry_id': entry_id})
                record_tool_result(self.repository, mission_id, turn_id, agent_id, 'memory',
                                   summary='forgotten', provider_id=provider_id)
                return {'id': entry_id, 'status': entry['status']}
            raise MCPToolError("memory 不支持该操作：{}".format(action))
        except MemoryError_ as exc:
            raise MCPToolError(str(exc)) from exc

    @extension_guard
    def save_checkpoint(self, mission_id, agent_id, turn_id, provider_id, text, source_message_ids=None):
        """Save the agent's rolling short-term checkpoint for this conversation."""
        from backend.app.orchestration.events import record_tool_call, record_tool_result
        from backend.app.memory import checkpoints as checkpoint_store
        from backend.app.memory.checkpoints import CheckpointError
        mission = self._require_mission(mission_id)
        if not mission.conversation_id or is_provider_verification_mission(mission):
            raise MCPToolError("检查点需要普通会话上下文")
        if not agent_id:
            raise MCPToolError("save_checkpoint 需要 agent_id")
        record_tool_call(self.repository, mission_id, turn_id, agent_id, 'save_checkpoint',
                         {'text_length': len(str(text or ''))}, provider_id)
        cursor = max((event.id for event in self.repository.list_events_after(mission_id, 0)), default=0)
        try:
            checkpoint = checkpoint_store.save(self.repository, mission.conversation_id, agent_id,
                                               text, source_message_ids or [], cursor_event_id=cursor)
        except CheckpointError as exc:
            raise MCPToolError(str(exc)) from exc
        self.repository.add_event(mission_id, 'checkpoint.saved', {
            'agent_id': agent_id, 'turn_id': turn_id,
            'source_message_ids': checkpoint['source_message_ids']})
        record_tool_result(self.repository, mission_id, turn_id, agent_id, 'save_checkpoint',
                           summary='saved', provider_id=provider_id)
        return checkpoint

    @extension_guard
    def publish_message(
        self,
        mission_id: str,
        agent_id: str,
        turn_id: str,
        content: str,
        provider_id: str,
        *,
        trace: bool = True,
    ) -> Message:
        mission = self._require_mission(mission_id)
        if trace:
            record_tool_call(
                self.repository,
                mission_id,
                turn_id,
                agent_id,
                "publish_message",
                {"content_length": len(content)},
                provider_id,
            )
        message = Message(
            id=f"msg_{uuid4().hex}",
            mission_id=mission_id,
            conversation_id=mission.conversation_id,
            role=MessageRole.AGENT,
            agent_id=agent_id,
            turn_id=turn_id,
            provider_id=provider_id,
            content=content,
        )
        self.repository.add_message(message)
        if self.receipt_id:
            self.repository.add_event(
                mission_id,
                "mcp.receipt",
                {
                    "receipt_id": self.receipt_id,
                    "message_id": message.id,
                    "turn_id": turn_id,
                    "agent_id": agent_id,
                    "provider_id": provider_id,
                    "tool": "publish_message",
                },
            )
        if trace:
            record_tool_result(
                self.repository,
                mission_id,
                turn_id,
                agent_id,
                "publish_message",
                summary="Published message {id}".format(id=message.id),
                provider_id=provider_id,
                output=content,
            )
        return message

    @extension_guard
    def write_artifact(
        self,
        mission_id: str,
        agent_id: str,
        turn_id: str,
        relative_path: str,
        content: Optional[str] = None,
        *,
        provider_id: Optional[str] = None,
        source_path: Optional[str] = None,
    ) -> Path:
        self._require_mission(mission_id)
        extension_points.each('tool_check', self, 'write_artifact', {'mission_id': mission_id})
        record_tool_call(
            self.repository,
            mission_id,
            turn_id,
            agent_id,
            "write_artifact",
            {"relative_path": relative_path},
            provider_id,
        )
        mission_dir = (self.artifact_root / mission_id).resolve()
        target = (mission_dir / relative_path).resolve()
        try:
            normalized_relative_path = target.relative_to(mission_dir).as_posix()
        except ValueError:
            record_tool_error(
                self.repository,
                mission_id,
                turn_id,
                agent_id,
                "write_artifact",
                "Artifact path is outside artifact directory",
                provider_id,
            )
            raise MCPToolError("Artifact path is outside artifact directory")
        try:
            if (content is None) == (source_path is None):
                raise MCPToolError("Provide exactly one of content or source_path")
            if source_path is not None:
                data = self._read_delivery_file(mission_id, source_path)
            elif isinstance(content, str):
                data = content.encode("utf-8")
            else:
                raise MCPToolError("content must be a string")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Replace atomically so a saved-file event never points at a partial write.
            temporary = target.with_name("." + target.name + "." + uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(data)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            record_tool_error(self.repository, mission_id, turn_id, agent_id,
                              "write_artifact", str(exc), provider_id)
            raise MCPToolError(str(exc)) from exc
        digest = hashlib.sha256(data).hexdigest()[:16]
        artifact_id = "{mission_id}:{path}".format(
            mission_id=mission_id,
            path=normalized_relative_path,
        )
        self.repository.add_event(
            mission_id,
            "artifact",
            {
                "mission_id": mission_id,
                "agent_id": agent_id,
                "turn_id": turn_id,
                "title": target.name,
                "path": normalized_relative_path,
                "provider_id": provider_id,
                "saved_at": utc_now().isoformat(),
            },
        )
        record_tool_result(
            self.repository,
            mission_id,
            turn_id,
            agent_id,
            "write_artifact",
            summary="Wrote {path}".format(path=normalized_relative_path),
            artifact_id=artifact_id,
            provider_id=provider_id,
            output="{path} · {n} bytes · sha256:{digest}".format(
                path=normalized_relative_path, n=len(data), digest=digest),
        )
        return target

    IMPORT_MAX_BYTES = 10 * 1024 * 1024

    def _read_delivery_file(self, mission_id: str, source_path: str) -> bytes:
        """Import bytes from the host workspace, respecting artifact conversation scope."""
        if not isinstance(source_path, str) or not source_path.strip():
            raise MCPToolError("source_path must be a non-empty string")
        source = (self.workspace_root / source_path).resolve()
        artifact_root = self.artifact_root.resolve()
        if source.is_relative_to(artifact_root):
            parts = source.relative_to(artifact_root).parts
            owner = self.repository.get_mission(parts[0]) if len(parts) > 1 else None
            current = self.repository.get_mission(mission_id)
            if not owner or (owner.id != mission_id and not (
                current.conversation_id and current.conversation_id == owner.conversation_id
            )):
                raise MCPToolError("Source artifact is not in the current conversation")
        elif not source.is_relative_to(self.workspace_root):
            raise MCPToolError("source_path is outside the workspace")
        # Reject directories/devices/FIFOs before reading, and bound memory use.
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise MCPToolError("source_path must refer to a regular file")
            if info.st_size > self.IMPORT_MAX_BYTES:
                raise MCPToolError("Source file exceeds the 10 MiB import limit")
            data = handle.read(self.IMPORT_MAX_BYTES + 1)
            if len(data) > self.IMPORT_MAX_BYTES:
                raise MCPToolError("Source file exceeds the 10 MiB import limit")
            return data

    READ_ARTIFACT_MAX_CHARS = 20000

    def read_artifact(
        self,
        mission_id: str,
        agent_id: str,
        turn_id: str,
        relative_path: str,
        *,
        provider_id: Optional[str] = None,
        with_line_numbers: bool = True,
        source_mission_id: Optional[str] = None,
        offset: int = 0,
        max_chars: int = 8000,
    ) -> Dict[str, object]:
        taken_over = extension_points.first('tool_override', self, 'read_artifact', {
            'mission_id': mission_id, 'agent_id': agent_id, 'turn_id': turn_id, 'relative_path': relative_path,
            'provider_id': provider_id, 'source_mission_id': source_mission_id, 'offset': offset, 'max_chars': max_chars})
        if taken_over is not None:
            return taken_over
        self._require_mission(mission_id)
        record_tool_call(
            self.repository,
            mission_id,
            turn_id,
            agent_id,
            "read_artifact",
            {"relative_path": relative_path, "with_line_numbers": with_line_numbers,
             "source_mission_id": source_mission_id},
            provider_id,
        )
        read_mission_id = mission_id
        if source_mission_id is not None:
            if not isinstance(source_mission_id, str) or not source_mission_id.strip():
                record_tool_error(self.repository, mission_id, turn_id, agent_id,
                                  "read_artifact", "source_mission_id must be a non-empty string", provider_id)
                raise MCPToolError("source_mission_id must be a non-empty string")
            read_mission_id = source_mission_id.strip()
            source = self.repository.get_mission(read_mission_id)
            current = self.repository.get_mission(mission_id)
            if source is None:
                record_tool_error(self.repository, mission_id, turn_id, agent_id,
                                  "read_artifact", f"Unknown source mission: {read_mission_id}", provider_id)
                raise MCPToolError(f"Unknown source mission: {read_mission_id}")
            if read_mission_id != mission_id and (
                current is None or not current.conversation_id
                or current.conversation_id != source.conversation_id
            ):
                record_tool_error(self.repository, mission_id, turn_id, agent_id,
                                  "read_artifact", "source mission is in a different conversation", provider_id)
                raise MCPToolError("source mission is in a different conversation")
        mission_dir = (self.artifact_root / read_mission_id).resolve()
        try:
            target = (mission_dir / relative_path).resolve()
            normalized = target.relative_to(mission_dir).as_posix()
        except ValueError:
            record_tool_error(
                self.repository,
                mission_id,
                turn_id,
                agent_id,
                "read_artifact",
                "Artifact path is outside artifact directory",
                provider_id,
            )
            raise MCPToolError("Artifact path is outside artifact directory")
        if not target.exists() or not target.is_file():
            record_tool_error(
                self.repository,
                mission_id,
                turn_id,
                agent_id,
                "read_artifact",
                "Artifact not found: {p}".format(p=normalized),
                provider_id,
            )
            raise MCPToolError("Artifact not found: {p}".format(p=normalized))
        raw = target.read_text(encoding="utf-8", errors="replace")
        total_chars = len(raw)
        truncated = total_chars > self.READ_ARTIFACT_MAX_CHARS
        body = raw[: self.READ_ARTIFACT_MAX_CHARS] if truncated else raw
        if with_line_numbers:
            lines = body.splitlines()
            rendered = "\n".join(
                "{n:>5}\t{line}".format(n=i + 1, line=line)
                for i, line in enumerate(lines)
            )
        else:
            rendered = body
        result: Dict[str, object] = {
            "mission_id": read_mission_id,
            "path": normalized,
            "content": rendered,
            "total_chars": total_chars,
            "returned_chars": len(body),
            "truncated": truncated,
        }
        record_tool_result(
            self.repository,
            mission_id,
            turn_id,
            agent_id,
            "read_artifact",
            summary="Read {p} ({n}/{t} chars{trunc})".format(
                p=normalized,
                n=len(body),
                t=total_chars,
                trunc=", truncated" if truncated else "",
            ),
            provider_id=provider_id,
            output=body,
        )
        return result

    @extension_guard
    def log_thought(
        self,
        mission_id: str,
        agent_id: str,
        turn_id: Optional[str],
        content: str,
        provider_id: Optional[str] = None,
        *,
        kind: Optional[str] = None,
        tags: Optional[List[str]] = None,
        trace: bool = True,
    ) -> Dict[str, object]:
        self._require_mission(mission_id)
        normalized_turn_id = (
            turn_id.strip()
            if isinstance(turn_id, str) and turn_id.strip()
            else "unknown"
        )
        normalized_kind = kind or "reasoning"
        valid_kinds = {"reasoning", "plan", "doubt", "decision"}
        if normalized_kind not in valid_kinds:
            valid_kind_list = ", ".join(sorted(valid_kinds))
            if trace:
                record_tool_error(
                    self.repository,
                    mission_id,
                    normalized_turn_id,
                    agent_id,
                    "log_thought",
                    "kind must be one of: {kinds}".format(kinds=valid_kind_list),
                    provider_id,
                )
            raise MCPToolError(
                "log_thought 'kind' must be one of: {kinds}".format(
                    kinds=valid_kind_list
                )
            )
        if not isinstance(content, str) or not content.strip():
            if trace:
                record_tool_error(
                    self.repository,
                    mission_id,
                    normalized_turn_id,
                    agent_id,
                    "log_thought",
                    "content is required",
                    provider_id,
                )
            raise MCPToolError("log_thought requires non-empty 'content'")
        normalized_tags: List[str] = []
        if tags is not None:
            if not isinstance(tags, list) or any(
                not isinstance(item, str) or not item.strip() for item in tags
            ):
                if trace:
                    record_tool_error(
                        self.repository,
                        mission_id,
                        normalized_turn_id,
                        agent_id,
                        "log_thought",
                        "tags must be a list of non-empty strings",
                        provider_id,
                    )
                raise MCPToolError(
                    "log_thought 'tags' must be a list of non-empty strings"
                )
            normalized_tags = [tag.strip() for tag in tags]
        if trace:
            record_tool_call(
                self.repository,
                mission_id,
                normalized_turn_id,
                agent_id,
                "log_thought",
                {
                    "content_length": len(content),
                    "kind": normalized_kind,
                    "tags": normalized_tags,
                },
                provider_id,
            )
        event_id = self.repository.add_event(
            mission_id,
            "agent.thought",
            {
                "mission_id": mission_id,
                "agent_id": agent_id,
                "turn_id": normalized_turn_id,
                "provider_id": provider_id,
                "kind": normalized_kind,
                "content": content,
                "tags": normalized_tags,
            },
        )
        if trace:
            record_tool_result(
                self.repository,
                mission_id,
                normalized_turn_id,
                agent_id,
                "log_thought",
                summary="Logged thought ({n} chars{tag_note})".format(
                    n=len(content),
                    tag_note=", tags=" + ",".join(normalized_tags) if normalized_tags else "",
                ),
                provider_id=provider_id,
                output=content,
            )
        return {"ok": True, "event_id": event_id}

    @extension_guard
    def update_status(
        self,
        mission_id: str,
        status: str,
        summary: Optional[str] = None,
        *,
        agent_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        mission = self._require_mission(mission_id)
        extension_points.each('tool_check', self, 'update_status', {
            'mission_id': mission_id, 'status': status, 'agent_id': agent_id})
        if agent_id and turn_id:
            record_tool_call(
                self.repository,
                mission_id,
                turn_id,
                agent_id,
                "update_status",
                {"status": status},
                provider_id,
            )
        payload = {"mission_id": mission.id, "status": status, "summary": summary}
        self.repository.add_event(mission_id, "status", payload)
        if agent_id and turn_id:
            record_tool_result(
                self.repository,
                mission_id,
                turn_id,
                agent_id,
                "update_status",
                summary="Status updated to {status}".format(status=status),
                provider_id=provider_id,
                output=summary or "",
            )

    @extension_guard
    def invoke_agent(
        self,
        mission_id: str,
        sender_agent_id: str,
        sender_turn_id: str,
        target_agent_id: str,
        purpose: str,
        provider_id: str,
        *,
        mode: str = "fire_and_forget",
    ) -> Dict[str, int]:
        mission = self._require_mission(mission_id)
        if isinstance(purpose, str) and len(purpose) > MAX_A2A_PURPOSE_CHARS:
            message = "purpose exceeds {n} characters; put details in an artifact and reference it".format(
                n=MAX_A2A_PURPOSE_CHARS)
            record_tool_error(self.repository, mission_id, sender_turn_id, sender_agent_id,
                              "invoke_agent", message, provider_id)
            raise MCPToolError(message)
        taken_over = extension_points.first('tool_override', self, 'invoke_agent', {
            'mission': mission, 'mission_id': mission_id, 'sender_agent_id': sender_agent_id,
            'sender_turn_id': sender_turn_id, 'target_agent_id': target_agent_id, 'purpose': purpose,
            'provider_id': provider_id, 'mode': mode})
        if taken_over is not None:
            return taken_over
        self._require_mission(mission_id)
        record_tool_call(
            self.repository,
            mission_id,
            sender_turn_id,
            sender_agent_id,
            "invoke_agent",
            {
                "target_agent_id": target_agent_id,
                "purpose": purpose,
                "mode": mode,
            },
            provider_id,
        )
        try:
            if mode not in ("fire_and_forget", "wait_reply"):
                raise MCPToolError("mode {mode} not supported".format(mode=mode))
            if not isinstance(target_agent_id, str) or not target_agent_id.strip():
                raise MCPToolError("target_agent_id is required")
            if self.valid_agent_ids and target_agent_id not in self.valid_agent_ids:
                raise MCPToolError("unknown agent: {id}".format(id=target_agent_id))
            if target_agent_id == sender_agent_id:
                raise MCPToolError("agent cannot invoke itself")
            if mode == "wait_reply" and self._peek_resume_chain is not None:
                chain = self._peek_resume_chain(sender_agent_id)
                if target_agent_id in chain:
                    raise MCPToolError(
                        "a2a wait cycle: {path}→{target}".format(
                            path="→".join(chain + [sender_agent_id]),
                            target=target_agent_id,
                        )
                    )
            if not isinstance(purpose, str) or not purpose.strip():
                raise MCPToolError("purpose is required")

            # Absorb a redundant fire-and-forget invoke when the target already
            # has a scheduled turn in the remaining workflow. Queuing an extra
            # a2a turn for someone about to speak makes them speak twice on the
            # same topic ("漂移/重复发言"). wait_reply is never skipped — the
            # sender is blocked on a real reply.
            if (
                mode == "fire_and_forget"
                and self._is_target_scheduled is not None
                and self._is_target_scheduled(target_agent_id)
            ):
                record_a2a_skipped(
                    self.repository,
                    mission_id,
                    sender_agent_id,
                    sender_turn_id,
                    target_agent_id,
                    purpose.strip(),
                    reason="target already scheduled in workflow",
                )
                record_tool_result(
                    self.repository,
                    mission_id,
                    sender_turn_id,
                    sender_agent_id,
                    "invoke_agent",
                    summary="Skipped {target}: already scheduled to speak".format(
                        target=target_agent_id,
                    ),
                    provider_id=provider_id,
                )
                return {"queued_position": 0, "skipped": True}

            if self.a2a_enqueue is None:
                # Subprocess path: one unit per accepted invocation. Accepted
                # ones are a2a.invoke events; this turn's requests are not
                # picked up yet. Skipped requests and resumes are free.
                if self.max_a2a_invocations > 0:
                    existing = self._subprocess_a2a_used(mission_id, sender_turn_id)
                    if existing >= self.max_a2a_invocations:
                        raise MCPToolError(
                            "a2a 深度耗尽 ({used}/{limit})".format(
                                used=existing,
                                limit=self.max_a2a_invocations,
                            )
                        )
                # Persist the request to the DB so the runner can pick it up
                # after this turn ends.
                record_a2a_request(
                    self.repository,
                    mission_id,
                    sender_agent_id,
                    sender_turn_id,
                    target_agent_id,
                    purpose.strip(),
                    mode,
                )
                queued_position = sum(
                    1
                    for e in self.repository.list_events_after(mission_id, 0)
                    if e.event_type == "a2a.request"
                    and e.payload.get("sender_turn_id") == sender_turn_id
                )
                record_tool_result(
                    self.repository,
                    mission_id,
                    sender_turn_id,
                    sender_agent_id,
                    "invoke_agent",
                    summary="Persisted a2a.request for {target} at position {pos}".format(
                        target=target_agent_id,
                        pos=queued_position,
                    ),
                    provider_id=provider_id,
                )
                total_used = self._subprocess_a2a_used(mission_id, sender_turn_id)
                remaining = (
                    max(0, self.max_a2a_invocations - total_used)
                    if self.max_a2a_invocations > 0
                    else -1
                )
                result = {"queued_position": queued_position, "estimated_remaining_a2a_budget": remaining}
                if mode == "wait_reply":
                    result["reply_pending"] = True
                    result["resume_position"] = queued_position + 1
                return result

            # In-process path: validate budget and enqueue immediately.
            used = self.a2a_budget_used()
            if used >= self.max_a2a_invocations:
                raise MCPToolError(
                    "a2a 深度耗尽 ({used}/{limit})".format(
                        used=used,
                        limit=self.max_a2a_invocations,
                    )
                )

            step = WorkflowStep(
                agent_id=target_agent_id,
                purpose=purpose.strip(),
                origin="a2a",
                sender_agent_id=sender_agent_id,
                sender_turn_id=sender_turn_id,
            )
            position = self.a2a_enqueue(step)
            queued_position = int(position) if position is not None else 1
            if mode == "wait_reply":
                resume_step = WorkflowStep(
                    agent_id=sender_agent_id,
                    purpose=resume_purpose(target_agent_id, purpose.strip()),
                    origin="a2a_resume",
                    sender_agent_id=sender_agent_id,
                    sender_turn_id=sender_turn_id,
                    parent_step_id=step.step_id,
                )
                self.a2a_enqueue(resume_step)
            record_a2a_invoke(
                self.repository,
                mission_id,
                sender_agent_id,
                sender_turn_id,
                target_agent_id,
                purpose.strip(),
                mode,
                queued_position,
                callee_step_id=step.step_id,
            )
            remaining = max(0, self.max_a2a_invocations - used - 1)
            record_tool_result(
                self.repository,
                mission_id,
                sender_turn_id,
                sender_agent_id,
                "invoke_agent",
                summary="Queued {target} at position {position}".format(
                    target=target_agent_id,
                    position=queued_position,
                ),
                provider_id=provider_id,
            )
            result = {
                "queued_position": queued_position,
                "estimated_remaining_a2a_budget": remaining,
            }
            if mode == "wait_reply":
                result["reply_pending"] = True
                result["resume_position"] = queued_position + 1
            return result
        except MCPToolError as exc:
            record_tool_error(
                self.repository,
                mission_id,
                sender_turn_id,
                sender_agent_id,
                "invoke_agent",
                str(exc),
                provider_id,
            )
            raise

    def _subprocess_a2a_used(self, mission_id: str, sender_turn_id: str) -> int:
        return sum(
            1
            for e in self.repository.list_events_after(mission_id, 0)
            if e.event_type == "a2a.invoke"
            or (e.event_type == "a2a.request" and e.payload.get("sender_turn_id") == sender_turn_id)
        )

    def _require_mission(self, mission_id: str):
        mission = self.repository.get_mission(mission_id)
        if mission is None:
            raise MCPToolError(f"Unknown mission: {mission_id}")
        return mission
