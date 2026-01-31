from __future__ import annotations

import logging
import json

from jarvis.formatting import as_blockquote, format_code_block, format_tool_path
from jarvis.messaging.messenger import Messenger
from jarvis.storage import Storage
from jarvis.verbosity import VerbosityManager

logger = logging.getLogger(__name__)

_TOOL_USE_NAME_MAP = {
    "bash": "执行命令",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "编辑文件",
    "list_files": "列出文件",
    "web_search": "网络搜索",
}


class CodexProgressHandler:
    def __init__(
        self,
        messenger: Messenger,
        storage: Storage,
        verbosity: VerbosityManager,
    ) -> None:
        self._messenger = messenger
        self._storage = storage
        self._verbosity = verbosity

    async def handle(self, chat_id: str, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                await self._storage.upsert_session(chat_id, str(thread_id))
            return

        if event_type == "event_msg":
            await self._handle_event_msg(chat_id, event.get("payload", {}))
            return

        if event_type == "item.completed":
            await self._handle_item_completed(chat_id, event.get("item", {}))
            return

    def _summarize_reasoning(self, text: str) -> str:
        return text

    async def _handle_event_msg(self, chat_id: str, payload: dict) -> None:
        msg_type = payload.get("type")
        if msg_type != "agent_reasoning":
            return
        reasoning_text = payload.get("text", "")
        if not reasoning_text:
            return
        summary = self._summarize_reasoning(reasoning_text)
        if not summary:
            return
        final_text = f"💭 思考\n{as_blockquote(summary)}"
        await self._messenger.send_markdown(chat_id, final_text, with_separator=False)

    async def _handle_item_completed(self, chat_id: str, item: dict) -> None:
        item_type = item.get("type")
        if item_type == "reasoning":
            await self._handle_item_reasoning(chat_id, item)
            return
        if item_type == "agent_message":
            return
        if item_type == "web_search":
            if not self._verbosity.show_tool_messages(chat_id):
                return
            tool_display = self._format_web_search_item(item)
            await self._messenger.send_markdown(
                chat_id,
                f"🔧 工具\n{tool_display}",
                with_separator=False,
            )
            return
        if item_type == "file_change":
            if not self._verbosity.show_tool_messages(chat_id):
                return
            tool_display = self._format_file_change_item(item)
            await self._messenger.send_markdown(
                chat_id,
                f"🔧 工具\n{tool_display}",
                with_separator=False,
            )
            return
        if item_type == "command_execution":
            if not self._verbosity.show_tool_messages(chat_id):
                return
            command = item.get("command", "")
            if command:
                await self._messenger.send_markdown(
                    chat_id,
                    format_code_block("⚙️ 执行命令", command),
                    with_separator=False,
                )
            return
        if item_type == "tool_use":
            if not self._verbosity.show_tool_messages(chat_id):
                return
            tool_name = item.get("name", "")
            tool_input = item.get("input", {})
            if tool_name:
                tool_display = self._format_tool_use(tool_name, tool_input)
                await self._messenger.send_markdown(
                    chat_id,
                    f"🔧 工具\n{tool_display}",
                    with_separator=False,
                )
                return

    async def _handle_item_reasoning(self, chat_id: str, item: dict) -> None:
        reasoning_text = ""
        item_text = item.get("text")
        if isinstance(item_text, str) and item_text:
            reasoning_text = item_text
        if not reasoning_text:
            summary_list = item.get("summary", [])
            reasoning_texts = [
                s.get("text", "")
                for s in summary_list
                if isinstance(s, dict) and s.get("type") == "summary_text" and s.get("text")
            ]
            if reasoning_texts:
                reasoning_text = "\n\n".join(reasoning_texts)

        if reasoning_text:
            summary = self._summarize_reasoning(reasoning_text)
            if summary:
                final_text = f"💭 思考\n{as_blockquote(summary)}"
                await self._messenger.send_markdown(chat_id, final_text, with_separator=False)
            return

        await self._messenger.send_markdown(chat_id, "💭 _思考中_...", with_separator=False)

    def _format_tool_use(self, tool_name: str, tool_input: dict) -> str:
        tool_display = _TOOL_USE_NAME_MAP.get(tool_name, tool_name)
        if tool_name == "bash" and "command" in tool_input:
            return format_code_block(tool_display, tool_input["command"])
        if "path" in tool_input:
            return format_tool_path(tool_display, str(tool_input["path"]))
        if "query" in tool_input:
            return format_tool_path(tool_display, str(tool_input["query"]))
        return tool_display

    def _format_web_search_call(self, action: dict) -> str:
        action_type = action.get("type") or "unknown"
        header = f"网络搜索（{action_type}）"
        if action_type == "search":
            query = action.get("query") or ""
            return f"{header}\n搜索: {query}".strip()
        if action_type == "open_page":
            url = action.get("url") or ""
            return f"{header}\n打开页面: {url}".strip()
        if action_type == "find_in_page":
            url = action.get("url") or ""
            pattern = action.get("pattern") or ""
            return f"{header}\n在 {url} 中查找 {pattern}".strip()
        return f"{header}\n{json.dumps(action, ensure_ascii=False)}"

    def _format_web_search_item(self, item: dict) -> str:
        action = item.get("action")
        if isinstance(action, dict):
            return self._format_web_search_call(action)
        for key in ("query", "q"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return f"网络搜索\n搜索: {value}".strip()
        payload = item.get("input") or item.get("params")
        if isinstance(payload, dict):
            query = payload.get("query") or payload.get("q")
            if isinstance(query, str) and query:
                return f"网络搜索\n搜索: {query}".strip()
            url = payload.get("url")
            pattern = payload.get("pattern")
            if isinstance(url, str) and isinstance(pattern, str):
                return f"网络搜索\n在 {url} 中查找 {pattern}".strip()
            if isinstance(url, str):
                return f"网络搜索\n打开页面: {url}".strip()
        return "网络搜索"

    def _format_file_change_item(self, item: dict) -> str:
        label = "文件变更"
        for key in ("path", "file"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return format_tool_path(label, value)
        for key in ("paths", "files"):
            value = item.get(key)
            if isinstance(value, list):
                paths = [str(p) for p in value if isinstance(p, str) and p]
                if paths:
                    return f"{label}\n" + "\n".join(paths)
        changes = item.get("changes")
        if isinstance(changes, list):
            paths = []
            for change in changes:
                if isinstance(change, dict):
                    path = change.get("path") or change.get("file")
                    if isinstance(path, str) and path:
                        paths.append(path)
            if paths:
                return f"{label}\n" + "\n".join(paths)
        return label
