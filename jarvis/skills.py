from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from jarvis.config import SkillSourceConfig


class SkillError(RuntimeError):
    pass


@dataclass(slots=True)
class RemoteSkillEntry:
    name: str
    source: str
    repo: str
    path: str
    ref: str | None


@dataclass(slots=True)
class InstalledSkillEntry:
    name: str
    description: str | None
    path: str


def resolve_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def resolve_skills_dir() -> Path:
    return resolve_codex_home() / "skills"


def _parse_frontmatter(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            payload = "\n".join(lines[1:idx])
            try:
                data = yaml.safe_load(payload) or {}
            except yaml.YAMLError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def list_installed_skills() -> list[InstalledSkillEntry]:
    skills_dir = resolve_skills_dir()
    if not skills_dir.exists():
        return []
    entries: list[InstalledSkillEntry] = []
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError:
            content = ""
        meta = _parse_frontmatter(content)
        name = str(meta.get("name") or child.name)
        desc = meta.get("description")
        desc_text = str(desc).strip() if desc else None
        entries.append(
            InstalledSkillEntry(
                name=name,
                description=desc_text,
                path=str(child),
            )
        )
    return entries


def resolve_source(sources: list[SkillSourceConfig], name: str) -> SkillSourceConfig | None:
    for source in sources:
        if source.name == name:
            return source
    return None


def _github_headers(source: SkillSourceConfig) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "jarvis-skill-client",
    }
    token_name = source.token_env or "GITHUB_TOKEN"
    token = os.environ.get(token_name) or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _github_get_json(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
) -> Any:
    async with session.get(url, headers=headers, params=params) as resp:
        text = await resp.text()
        if resp.status >= 400:
            raise SkillError(f"GitHub 请求失败: {resp.status} {text[:200]}")
        try:
            return await resp.json()
        except Exception:
            raise SkillError("GitHub 返回非 JSON 响应")


async def list_remote_skills(
    sources: list[SkillSourceConfig],
    source_name: str | None = None,
) -> list[RemoteSkillEntry]:
    targets = sources
    if source_name:
        source = resolve_source(sources, source_name)
        if not source:
            raise SkillError(f"未找到 skill source: {source_name}")
        targets = [source]

    entries: list[RemoteSkillEntry] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        for source in targets:
            if source.type != "github":
                raise SkillError(f"暂不支持的 source 类型: {source.type}")
            if not source.repo or not source.path:
                raise SkillError(f"source 配置缺少 repo/path: {source.name}")
            headers = _github_headers(source)
            url = f"https://api.github.com/repos/{source.repo}/contents/{source.path}"
            params = {"ref": source.ref} if source.ref else None
            data = await _github_get_json(session, url, headers, params=params)
            if isinstance(data, dict):
                raise SkillError(f"source 路径不是目录: {source.name}")
            if not isinstance(data, list):
                raise SkillError(f"source 返回未知格式: {source.name}")
            for item in data:
                if item.get("type") != "dir":
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                entries.append(
                    RemoteSkillEntry(
                        name=name,
                        source=source.name,
                        repo=source.repo,
                        path=f"{source.path}/{name}",
                        ref=source.ref,
                    )
                )
    return entries


async def _download_github_dir(
    session: aiohttp.ClientSession,
    source: SkillSourceConfig,
    repo_path: str,
    dest: Path,
) -> None:
    headers = _github_headers(source)
    url = f"https://api.github.com/repos/{source.repo}/contents/{repo_path}"
    params = {"ref": source.ref} if source.ref else None
    data = await _github_get_json(session, url, headers, params=params)
    if isinstance(data, dict):
        raise SkillError(f"路径不是目录: {repo_path}")
    if not isinstance(data, list):
        raise SkillError(f"未知目录返回: {repo_path}")

    dest.mkdir(parents=True, exist_ok=True)

    for item in data:
        item_type = item.get("type")
        name = item.get("name")
        if not name:
            continue
        if item_type == "dir":
            await _download_github_dir(session, source, f"{repo_path}/{name}", dest / name)
            continue
        if item_type != "file":
            continue
        target = dest / name
        download_url = item.get("download_url")
        if download_url:
            async with session.get(download_url, headers=headers) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise SkillError(f"下载失败: {resp.status} {text[:200]}")
                target.write_bytes(await resp.read())
            continue
        raw_headers = dict(headers)
        raw_headers["Accept"] = "application/vnd.github.raw"
        async with session.get(item.get("url"), headers=raw_headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise SkillError(f"下载失败: {resp.status} {text[:200]}")
            target.write_bytes(await resp.read())


async def install_skill(
    sources: list[SkillSourceConfig],
    source_name: str,
    skill_name: str,
) -> Path:
    source = resolve_source(sources, source_name)
    if not source:
        raise SkillError(f"未找到 skill source: {source_name}")
    if source.type != "github":
        raise SkillError(f"暂不支持的 source 类型: {source.type}")
    if not source.repo or not source.path:
        raise SkillError(f"source 配置缺少 repo/path: {source.name}")
    skill_root = resolve_skills_dir()
    skill_root.mkdir(parents=True, exist_ok=True)
    dest = skill_root / skill_name
    if dest.exists():
        raise SkillError(f"技能已存在: {dest}")
    repo_path = f"{source.path}/{skill_name}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        await _download_github_dir(session, source, repo_path, dest)
    return dest
