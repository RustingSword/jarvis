from __future__ import annotations

import json
import logging

from jarvis.formatting import as_blockquote, format_code_block, format_tool_path
from jarvis.messaging.messenger import Messenger
from jarvis.storage import Storage
from jarvis.verbosity import VerbosityManager

logger = logging.getLogger(__name__)

_TOOL_CALL_NAME_MAP = {
    "shell_command": "执行命令",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "编辑文件",
    "list_directory": "列出目录",
    "web_search": "网络搜索",
    "browser_action": "浏览器操作",
}

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

        if event_type == "response_item":
            await self._handle_response_item(chat_id, event.get("payload", {}))
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

    async def _handle_response_item(self, chat_id: str, payload: dict) -> None:
        if not self._verbosity.show_tool_messages(chat_id):
            return
        if payload.get("type") != "function_call":
            return
        tool_name = payload.get("name", "")
        arguments = payload.get("arguments", "")
        tool_display = self._format_tool_call(tool_name, arguments)
        await self._messenger.send_markdown(chat_id, f"🔧 工具\n{tool_display}", with_separator=False)

    async def _handle_item_completed(self, chat_id: str, item: dict) -> None:
        item_type = item.get("type")
        if item_type == "reasoning":
            await self._handle_item_reasoning(chat_id, item)
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

    def _format_tool_call(self, tool_name: str, arguments: str) -> str:
        tool_display = _TOOL_CALL_NAME_MAP.get(tool_name, tool_name)
        try:
            args = json.loads(arguments)
            if tool_name == "shell_command" and "command" in args:
                cmd = args["command"]
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                return format_code_block(tool_display, cmd_str)
            if "path" in args:
                return format_tool_path(tool_display, str(args["path"]))
            if "file" in args:
                return format_tool_path(tool_display, str(args["file"]))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        return tool_display

    def _format_tool_use(self, tool_name: str, tool_input: dict) -> str:
        tool_display = _TOOL_USE_NAME_MAP.get(tool_name, tool_name)
        if tool_name == "bash" and "command" in tool_input:
            return format_code_block(tool_display, tool_input["command"])
        if "path" in tool_input:
            return format_tool_path(tool_display, str(tool_input["path"]))
        if "query" in tool_input:
            return format_tool_path(tool_display, str(tool_input["query"]))
        return tool_display
