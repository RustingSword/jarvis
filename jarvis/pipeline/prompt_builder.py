from __future__ import annotations

from loguru import logger

from jarvis.memory import MemoryManager


class PromptBuilder:
    def __init__(self, memory: MemoryManager) -> None:
        self._memory = memory

    async def build(self, text: str, attachments: list[dict]) -> str:
        base_text = text or ""
        prompt = await self._augment_with_memory(base_text) if base_text else ""
        if attachments:
            attachments_text = self._format_attachments_prompt(attachments)
            if prompt:
                prompt = f"{prompt}\n\n{attachments_text}"
            else:
                prompt = f"用户未提供文本，仅提供了附件。\n\n{attachments_text}"
        final_prompt = prompt or base_text
        if final_prompt:
            return f"{_tts_guidance()}\n\n{final_prompt}"
        return _tts_guidance()

    async def _augment_with_memory(self, text: str) -> str:
        if not self._memory.enabled:
            return text
        try:
            results = await self._memory.search(text)
        except Exception:
            logger.exception("Memory search failed")
            return text
        if not results:
            return text
        lines = ["以下是可能相关的记忆片段（仅供参考）："]
        for idx, item in enumerate(results, start=1):
            lines.append(f"{idx}. {item.path}#L{item.start_line}-L{item.end_line}: {item.snippet}")
        lines.append("")
        lines.append("用户消息：")
        lines.append(text)
        return "\n".join(lines)

    @staticmethod
    def _format_attachments_prompt(attachments: list[dict]) -> str:
        lines = ["用户附件（请直接读取以下文件路径）："]
        for idx, item in enumerate(attachments, start=1):
            path = item.get("path") or item.get("file") or ""
            if not path:
                continue
            meta_parts = []
            item_type = item.get("type")
            if item_type:
                meta_parts.append(str(item_type))
            file_name = item.get("file_name")
            if file_name:
                meta_parts.append(str(file_name))
            mime_type = item.get("mime_type")
            if mime_type:
                meta_parts.append(str(mime_type))
            meta = f" ({' / '.join(meta_parts)})" if meta_parts else ""
            lines.append(f"{idx}. {path}{meta}")
        return "\n".join(lines)


def _tts_guidance() -> str:
    return (
        "系统提示：如需语音播报，请将要播报的部分用 `<tts>...</tts>` 包裹，"
        "系统会提取该内容生成语音并从文本回复中移除，不要向用户解释该标签。"
        "若为故事类回复，请将完整故事置于 `<tts>...</tts>` 中。"
    )
