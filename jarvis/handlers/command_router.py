from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from jarvis.codex import CodexError, CodexManager, CodexProcessError, CodexTimeoutError
from jarvis.config import SkillsConfig, SkillSourceConfig, persist_skill_source
from jarvis.event_bus import Event
from jarvis.formatting import format_code_block
from jarvis.memory import MemoryManager
from jarvis.messaging.messenger import Messenger
from jarvis.pipeline.prompt_builder import PromptBuilder
from jarvis.skills import SkillError, install_skill, list_installed_skills, list_remote_skills
from jarvis.storage import Storage
from jarvis.verbosity import VerbosityManager

logger = logging.getLogger(__name__)

EventEnqueuer = Callable[[Event], Awaitable[None]]


class CommandRouter:
    def __init__(
        self,
        messenger: Messenger,
        storage: Storage,
        codex: CodexManager,
        memory: MemoryManager,
        skills: SkillsConfig,
        config_path: str | None,
        verbosity: VerbosityManager,
        enqueue_task: EventEnqueuer | None = None,
    ) -> None:
        self._messenger = messenger
        self._storage = storage
        self._codex = codex
        self._memory = memory
        self._skills = skills
        self._config_path = config_path
        self._verbosity = verbosity
        self._enqueue_task = enqueue_task
        self._prompt_builder = PromptBuilder(memory)

        self._handlers = {
            "start": self._cmd_start,
            "help": self._cmd_help,
            "new": self._cmd_new,
            "reset": self._cmd_reset,
            "compact": self._cmd_compact,
            "resume": self._cmd_resume,
            "verbosity": self._cmd_verbosity,
            "skills": self._cmd_skills,
            "memory": self._cmd_memory,
        }

    async def handle(self, event: Event) -> None:
        chat_id = event.payload.get("chat_id")
        command = event.payload.get("command")
        args = event.payload.get("args", [])
        if not chat_id or not command:
            return
        await self._verbosity.ensure(chat_id)
        handler = self._handlers.get(command)
        if not handler:
            await self._messenger.send_markdown(chat_id, f"未知命令: `{command}`")
            return
        await handler(chat_id, args)

    async def _cmd_start(self, chat_id: str, args: list[str]) -> None:
        await self._messenger.send_markdown(chat_id, "你好，输入消息即可对话。")

    async def _cmd_help(self, chat_id: str, args: list[str]) -> None:
        await self._messenger.send_markdown(
            chat_id,
            "\n".join(
                [
                    "**可用命令**",
                    "- `/start` - 开始对话",
                    "- `/help` - 显示帮助",
                    "- `/new [任务]` - 新建会话（可直接跟任务并执行）",
                    "- `/reset` - 重置当前对话上下文",
                    "- `/compact` - 压缩对话历史并重置",
                    "- `/resume <id>` - 恢复历史会话（不带 id 会列出最近会话）",
                    "- `/verbosity <full|compact|result|reset>` - 控制输出详细程度",
                    (
                        "- `/skills sources` | `/skills list [source]` | `/skills installed` | "
                        "`/skills install <source> <name>` | "
                        "`/skills add-source <name> <repo> <path> "
                        "[ref] [token_env]` - skills 管理"
                    ),
                    (
                        "- `/memory search <关键词>` | `/memory add <内容>` | "
                        "`/memory get <path> [from] [lines]` | "
                        "`/memory index` | `/memory status` - 记忆功能"
                    ),
                    "",
                    "提示：每条消息前会显示会话标识，如 `> [12]`。",
                ]
            ),
        )

    async def _cmd_new(self, chat_id: str, args: list[str]) -> None:
        await self._storage.clear_session(chat_id)
        if not args:
            await self._messenger.send_markdown(chat_id, "已创建新会话，请发送新消息开始。")
            return
        task_text = " ".join(args).strip()
        if not task_text:
            await self._messenger.send_markdown(chat_id, "已创建新会话，请发送新消息开始。")
            return
        if self._enqueue_task:
            event = Event(
                type="command.task",
                payload={"chat_id": str(chat_id), "task": task_text},
                created_at=datetime.now(timezone.utc),
            )
            await self._enqueue_task(event)
            await self._messenger.send_markdown(
                chat_id,
                "任务已进入队列，开始执行后会提示会话 ID。",
                with_session_prefix=False,
            )
            return
        try:
            prompt = await self._prompt_builder.build(task_text, [])
            result = await self._codex.run(prompt)
        except CodexTimeoutError:
            await self._messenger.send_markdown(chat_id, "新会话任务执行超时，请稍后再试。")
            return
        except CodexProcessError as exc:
            await self._messenger.send_markdown(chat_id, f"新会话任务执行失败: {exc}")
            return

        session_record = None
        if result.thread_id:
            session_record = await self._storage.upsert_session(
                chat_id, result.thread_id, set_active=True
            )

        response_text = result.response_text.strip() if result.response_text else "(无可用回复)"
        await self._messenger.send_markdown(
            chat_id,
            response_text,
            session_id=session_record.session_id if session_record else None,
            thread_id=session_record.thread_id if session_record else None,
        )

    async def _cmd_reset(self, chat_id: str, args: list[str]) -> None:
        await self._storage.clear_session(chat_id)
        await self._messenger.send_markdown(chat_id, "会话已重置。")

    async def _cmd_compact(self, chat_id: str, args: list[str]) -> None:
        await self._handle_compact(chat_id)

    async def _cmd_resume(self, chat_id: str, args: list[str]) -> None:
        if not args or not args[0].isdigit():
            sessions = await self._storage.list_sessions(chat_id, limit=5)
            if not sessions:
                await self._messenger.send_markdown(chat_id, "暂无可恢复的会话。")
                return
            active_session = await self._storage.get_session(chat_id)
            active_id = active_session.session_id if active_session else None
            lines = ["**用法**: `/resume <id>`", "**最近会话**:"]
            for session in sessions:
                ts = _format_local_time(session.last_active)
                marker = "*" if active_id == session.session_id else ""
                lines.append(f"- {session.session_id}{marker} (最后活动: {ts})")
            await self._messenger.send_markdown(chat_id, "\n".join(lines))
            return

        session_id = int(args[0])
        record = await self._storage.activate_session(chat_id, session_id)
        if not record:
            await self._messenger.send_markdown(chat_id, f"未找到会话 ID: `{session_id}`")
            return
        await self._messenger.send_markdown(chat_id, "已恢复会话。")

    async def _cmd_verbosity(self, chat_id: str, args: list[str]) -> None:
        if not args:
            current = self._verbosity.get(chat_id)
            await self._messenger.send_markdown(
                chat_id,
                f"**当前 verbosity**: `{current}`\n"
                f"**用法**: `/verbosity full|compact|result|reset`",
            )
            return

        action = args[0].strip().lower()
        if action in {"reset", "default"}:
            await self._verbosity.reset(chat_id)
            await self._messenger.send_markdown(
                chat_id,
                f"verbosity 已重置为默认值: `{self._verbosity.default}`",
            )
            return

        try:
            normalized = await self._verbosity.set(chat_id, args[0])
        except ValueError:
            await self._messenger.send_markdown(
                chat_id, "**用法**: `/verbosity full|compact|result|reset`"
            )
            return

        await self._messenger.send_markdown(chat_id, f"verbosity 已设置为: `{normalized}`")

    async def _handle_compact(self, chat_id: str) -> None:
        session = await self._storage.get_session(chat_id)
        if not session:
            await self._messenger.send_markdown(chat_id, "当前没有可压缩的会话。")
            return
        try:
            summary_result = await self._codex.run(
                "请总结到目前为止的对话内容，包含关键上下文、决策与待办事项，"
                "用简洁的要点列出，控制在 200 字以内。",
                session_id=session.thread_id,
            )
        except CodexTimeoutError:
            await self._messenger.send_markdown(chat_id, "会话压缩超时，请稍后再试。")
            return
        except CodexProcessError as exc:
            error_msg = str(exc)
            if "UTF-8" in error_msg:
                error_msg = f"会话文件可能已损坏。建议使用 `/reset` 重置会话。\n技术详情: {exc}"
            await self._messenger.send_markdown(chat_id, f"会话压缩失败: {error_msg}")
            return

        summary = summary_result.response_text.strip()
        if not summary:
            await self._messenger.send_markdown(chat_id, "未获取到摘要内容，压缩失败。")
            return

        try:
            title = "compact"
            if session.session_id is not None:
                title = f"compact session_id={session.session_id}"
            await self._memory.append_daily_block(summary, title=title)
            await self._memory.sync()
        except Exception:
            logger.exception("Failed to write compact summary to memory")

        await self._storage.save_summary(chat_id, summary)
        await self._storage.clear_session(chat_id)

        seed_prompt = "以下是之前对话的摘要，请基于这些内容继续后续对话：\n" + summary
        try:
            seed_result = await self._codex.run(seed_prompt)
        except CodexError:
            seed_result = None

        if seed_result and seed_result.thread_id:
            await self._storage.upsert_session(chat_id, seed_result.thread_id)

        await self._messenger.send_markdown(chat_id, "会话已压缩并重置。")
        try:
            await self._maybe_consolidate_yesterday_memory()
        except Exception:
            logger.exception("Failed to consolidate yesterday memory")

    async def _maybe_consolidate_yesterday_memory(self) -> None:
        if not self._memory.enabled:
            return
        workspace = self._memory.workspace_dir
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        state_path = memory_dir / ".state.json"
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8")) or {}
            except Exception:
                state = {}
        yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
        if state.get("last_consolidated") == yesterday:
            return
        yesterday_path = memory_dir / f"{yesterday}.md"
        if not yesterday_path.exists():
            return
        raw = yesterday_path.read_text(encoding="utf-8").strip()
        if not raw:
            state["last_consolidated"] = yesterday
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            return
        content = _truncate_text(raw, 4000)
        prompt = (
            "你是 Jarvis 的记忆整理器。请从下面的“昨日记忆”中提炼适合长期记忆的要点，"
            "输出 3-8 条精炼的项目符号（每条不超过 30 字）。"
            "如果没有值得长期保留的内容，输出 NO_UPDATE。\n\n"
            f"昨日记忆（{yesterday}）:\n{content}\n"
        )
        result = await self._codex.run(prompt)
        response = (result.response_text or "").strip()
        if not response or response.upper().startswith("NO_UPDATE"):
            state["last_consolidated"] = yesterday
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            return
        await self._memory.append_global_block(response, title=f"{yesterday} consolidate")
        await self._memory.sync()
        state["last_consolidated"] = yesterday
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    async def _cmd_skills(self, chat_id: str, args: list[str]) -> None:
        if not args:
            await self._messenger.send_markdown(chat_id, _format_skills_usage())
            return

        action = args[0]
        if action == "installed":
            installed = list_installed_skills()
            if not installed:
                await self._messenger.send_markdown(chat_id, "暂无已安装技能。")
                return
            lines = ["**已安装技能**"]
            for entry in installed:
                desc = f" - {entry.description}" if entry.description else ""
                lines.append(f"- `{entry.name}`{desc}")
            await self._messenger.send_markdown(chat_id, "\n".join(lines))
            return

        if action == "sources":
            sources = self._skills.sources
            if not sources:
                await self._messenger.send_markdown(chat_id, "未配置 skills sources。")
                return
            lines = ["**已配置 sources**"]
            for src in sources:
                ref = f"@{src.ref}" if src.ref else ""
                target = f"{src.repo}/{src.path}{ref}"
                lines.append(f"- `{src.name}`: {src.type} `{target}`")
            await self._messenger.send_markdown(chat_id, "\n".join(lines))
            return

        if action == "list":
            sources = self._skills.sources
            if not sources:
                await self._messenger.send_markdown(chat_id, "未配置 skills sources。")
                return
            source_name = args[1] if len(args) > 1 else None
            try:
                remote = await list_remote_skills(sources, source_name=source_name)
            except SkillError as exc:
                await self._messenger.send_markdown(chat_id, f"skills 列表获取失败: {exc}")
                return
            if not remote:
                await self._messenger.send_markdown(chat_id, "未找到可用技能。")
                return
            installed_names = {entry.name for entry in list_installed_skills()}
            grouped: dict[str, list[str]] = {}
            for entry in remote:
                label = entry.source
                name = entry.name
                if name in installed_names:
                    name = f"{name} (已安装)"
                grouped.setdefault(label, []).append(name)
            lines = ["**可用技能**"]
            for label, items in grouped.items():
                lines.append(f"**{label}**")
                for idx, item in enumerate(items, start=1):
                    lines.append(f"{idx}. `{item}`")
            await self._messenger.send_markdown(chat_id, "\n".join(lines))
            return

        if action == "install":
            if len(args) < 3:
                await self._messenger.send_markdown(
                    chat_id, "**用法**: `/skills install <source> <name>`"
                )
                return
            source_name = args[1]
            skill_name = args[2]
            try:
                dest = await install_skill(self._skills.sources, source_name, skill_name)
            except SkillError as exc:
                await self._messenger.send_markdown(chat_id, f"安装失败: {exc}")
                return
            await self._messenger.send_markdown(chat_id, f"已安装 `{skill_name}` -> `{dest}`")
            return

        if action == "add-source":
            if len(args) < 4:
                await self._messenger.send_markdown(
                    chat_id,
                    "**用法**: `/skills add-source <name> <repo> <path> [ref] [token_env]`",
                )
                return
            if not self._config_path:
                await self._messenger.send_markdown(chat_id, "未找到配置路径，无法持久化 source。")
                return
            name = args[1].strip()
            repo = args[2].strip()
            path = args[3].strip()
            ref = args[4].strip() if len(args) > 4 else None
            token_env = args[5].strip() if len(args) > 5 else None
            if not name or not repo or not path:
                await self._messenger.send_markdown(
                    chat_id,
                    "**用法**: `/skills add-source <name> <repo> <path> [ref] [token_env]`",
                )
                return
            source = SkillSourceConfig(
                name=name,
                type="github",
                repo=repo,
                path=path,
                ref=ref or None,
                token_env=token_env or None,
            )
            try:
                updated = persist_skill_source(self._config_path, source)
            except Exception as exc:
                await self._messenger.send_markdown(chat_id, f"写入配置失败: {exc}")
                return

            replaced = False
            for idx, entry in enumerate(self._skills.sources):
                if entry.name == name:
                    self._skills.sources[idx] = source
                    replaced = True
                    break
            if not replaced:
                self._skills.sources.append(source)

            action_label = "已更新" if updated else "已添加"
            await self._messenger.send_markdown(chat_id, f"{action_label} source: `{name}`")
            return

        await self._messenger.send_markdown(chat_id, "未知 skills 子命令。")

    async def _cmd_memory(self, chat_id: str, args: list[str]) -> None:
        if not self._memory.enabled:
            await self._messenger.send_markdown(chat_id, "记忆功能已禁用。")
            return
        if not args:
            await self._messenger.send_markdown(
                chat_id,
                "**用法**: `/memory search <关键词>` | `/memory add <内容>` | "
                "`/memory get <path> [from] [lines]` | `/memory index` | `/memory status`",
            )
            return
        action = args[0].strip().lower()
        if action == "search":
            query = " ".join(args[1:]).strip()
            if not query:
                await self._messenger.send_markdown(chat_id, "**用法**: `/memory search <关键词>`")
                return
            try:
                results = await self._memory.search(query)
            except Exception:
                logger.exception("Memory search failed")
                await self._messenger.send_markdown(chat_id, "记忆搜索失败。")
                return
            if not results:
                await self._messenger.send_markdown(chat_id, "没有找到相关记忆。")
                return
            lines = ["**搜索结果**:"]
            for item in results:
                lines.append(f"- `{item.path}` L{item.start_line}-L{item.end_line}: {item.snippet}")
            await self._messenger.send_markdown(chat_id, "\n".join(lines))
            return

        if action == "add":
            content = " ".join(args[1:]).strip()
            if not content:
                await self._messenger.send_markdown(chat_id, "**用法**: `/memory add <内容>`")
                return
            try:
                path = await self._memory.append_daily(content)
                await self._memory.sync()
            except Exception:
                logger.exception("Memory append failed")
                await self._messenger.send_markdown(chat_id, "记忆写入失败。")
                return
            if path:
                await self._messenger.send_markdown(chat_id, f"已写入记忆：`{path}`")
            else:
                await self._messenger.send_markdown(chat_id, "未写入内容。")
            return

        if action == "get":
            if len(args) < 2:
                await self._messenger.send_markdown(
                    chat_id, "**用法**: `/memory get <path> [from] [lines]`"
                )
                return
            path = args[1]
            from_line = None
            lines_count = None
            if len(args) >= 3 and args[2].isdigit():
                from_line = int(args[2])
            if len(args) >= 4 and args[3].isdigit():
                lines_count = int(args[3])
            try:
                snippet = await self._memory.read_snippet(path, from_line, lines_count)
            except Exception:
                logger.exception("Memory read failed")
                await self._messenger.send_markdown(chat_id, "记忆读取失败。")
                return
            await self._messenger.send_markdown(chat_id, format_code_block(f"📄 {path}", snippet))
            return

        if action == "index":
            try:
                await self._memory.sync(force=True)
            except Exception:
                logger.exception("Memory reindex failed")
                await self._messenger.send_markdown(chat_id, "记忆索引失败。")
                return
            await self._messenger.send_markdown(chat_id, "记忆索引已更新。")
            return

        if action == "status":
            try:
                stats = await self._memory.status()
            except Exception:
                logger.exception("Memory status failed")
                await self._messenger.send_markdown(chat_id, "记忆状态获取失败。")
                return
            await self._messenger.send_markdown(
                chat_id, f"**记忆状态**\n- files: {stats['files']}\n- chunks: {stats['chunks']}"
            )
            return

        await self._messenger.send_markdown(chat_id, "未知 memory 子命令。")


def _format_skills_usage() -> str:
    return "\n".join(
        [
            "**用法**",
            "- `/skills sources`",
            "- `/skills list [source]`",
            "- `/skills installed`",
            "- `/skills install <source> <name>`",
            "- `/skills add-source <name> <repo> <path> [ref] [token_env]`",
        ]
    )


def _truncate_text(text: str, max_chars: int) -> str:
    limit = max(50, max_chars)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...(truncated)"


def _format_local_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().isoformat(sep=" ", timespec="minutes")
