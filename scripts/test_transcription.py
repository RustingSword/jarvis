#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import aiohttp


def _pick_output_path() -> Path:
    base_dir = Path(__file__).resolve().parent / "tmp"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "transcription_test.wav"


def _generate_audio(target: Path, text: str) -> bool:
    if shutil.which("espeak"):
        try:
            subprocess.run(
                ["espeak", "-w", str(target), text],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except subprocess.CalledProcessError:
            return False
    if shutil.which("say"):
        try:
            subprocess.run(
                ["say", "-o", str(target), text],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except subprocess.CalledProcessError:
            return False
    return False


async def _transcribe(
    file_path: Path,
    *,
    api_key: str,
    base_url: str,
    model: str,
    response_format: str,
    timeout: float,
) -> str:
    url = base_url.rstrip("/") + "/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        with file_path.open("rb") as handle:
            form = aiohttp.FormData()
            form.add_field("file", handle, filename=file_path.name)
            form.add_field("model", model)
            if response_format:
                form.add_field("response_format", response_format)
            async with session.post(url, data=form, headers=headers) as resp:
                if resp.status >= 400:
                    detail = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {detail}")
                if response_format in {"json", "verbose_json"}:
                    payload = await resp.json()
                    text = payload.get("text") if isinstance(payload, dict) else None
                    return str(text or "").strip()
                return (await resp.text()).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone OpenAI audio transcription test.")
    parser.add_argument("--file", type=str, help="Path to audio file to transcribe.")
    parser.add_argument("--model", type=str, default="whisper-1", help="Transcription model.")
    parser.add_argument("--response-format", type=str, default="json", help="Response format.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Request timeout seconds.")
    args = parser.parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Missing OPENAI_API_KEY in environment.", file=sys.stderr)
        return 2
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com")

    if args.file:
        file_path = Path(args.file).expanduser()
        if not file_path.exists():
            print(f"Audio file not found: {file_path}", file=sys.stderr)
            return 2
    else:
        file_path = _pick_output_path()
        if not _generate_audio(file_path, "Hello, this is a transcription test."):
            print(
                "No audio generator found (espeak/say). Please pass --file with an audio path.",
                file=sys.stderr,
            )
            return 2

    print(f"Using audio file: {file_path}")
    try:
        text = asyncio.run(
            _transcribe(
                file_path,
                api_key=api_key,
                base_url=base_url,
                model=args.model,
                response_format=args.response_format,
                timeout=args.timeout,
            )
        )
    except Exception as exc:
        print(f"Transcription failed: {exc}", file=sys.stderr)
        return 1

    print("Transcription result:")
    print(text or "(empty)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
