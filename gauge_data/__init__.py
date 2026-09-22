"""GAUGE 的 Codex 本地只读数据层。

本模块只读取 Codex 的状态库、历史库和 rollout JSONL；它不调用网络，
也不向 Codex 源库写入任何内容。对日志正文只解析白名单事件和字段，
避免把对话、命令参数或工具返回内容带出数据层。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote

try:  # Python 3.11+；缺失时仍可读取 JSON 配置和数据库。
    import tomllib
except ImportError:  # pragma: no cover - 仅支持旧版 Python 的降级路径
    tomllib = None  # type: ignore[assignment]

__all__ = ["collect_codex", "codex_widget"]

# v2：v1 缓存曾在现代格式文件（token_usage_record 与 token_count 并存，
# 实测 133/136 同文件）里无条件派生 derived 记录，造成每响应双计污染；
# schema_version 不匹配会让 _load_cache 整体重扫重建，一次性成本可接受。
_CACHE_VERSION = 2
_CACHE_NAME_PREFIX = "codex-cache-"
_CACHE_LOCK = threading.RLock()
_CACHE_LOCK_TIMEOUT = 1.2
_TOOL_TYPES = {
    "commandExecution",
    "mcpToolCall",
    "collabAgentToolCall",
    "dynamicToolCall",
    "fileChange",
}
_FAILURE_STATUSES = {
    "error",
    "failed",
    "failure",
    "cancelled",
    "canceled",
    "aborted",
    "timeout",
}
_CACHE_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_text(value: Any, limit: int = 240) -> str | None:
    """把展示所需的短文本限制为安全摘要，不保留正文和控制字符。"""
    if value is None:
        return None
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        text = str(value)
    else:
        text = str(value)
    text = " ".join(text.replace("\x00", "").split())
    return text[:limit] or None


def _as_id(value: Any) -> str | None:
    text = _safe_text(value, 256)
    return text


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return default


def _timestamp_ms(value: Any) -> int | None:
    """把秒、毫秒、datetime 和 ISO 时间统一为毫秒时间戳。"""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
        return int(dt.timestamp() * 1000)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
            if abs(number) < 100_000_000_000:
                number *= 1000
            return int(number)
        except (TypeError, ValueError, OverflowError):
            return None
    text = _safe_text(value, 100)
    if not text:
        return None
    try:
        return _timestamp_ms(float(text))
    except (ValueError, TypeError):
        pass
    try:
        iso = text.replace("Z", "+00:00")
        parsed = _dt.datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
        return int(parsed.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso(ms: int | None = None) -> str:
    if ms is None:
        ms = _now_ms()
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.datetime.now().astimezone().tzinfo).isoformat(
        timespec="milliseconds"
    )


def _json_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:
            return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return None


def _dedup(items: Iterable[Any], key: str) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for item in items:
        value = item.get(key) if isinstance(item, dict) else None
        marker = value if value is not None else id(item)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _empty_snapshot(now_ms: int | None = None) -> dict[str, Any]:
    current_ms = now_ms or _now_ms()
    return {
        "platform": "codex",
        "available": False,
        "status": "unavailable",
        "generated_at": _now_iso(current_ms),
        "generated_at_ms": current_ms,
        "sources": {},
        "coverage": {
            "processed_files": 0,
            "total_files": 0,
            "pending_bytes": 0,
            "usage_records": 0,
            "complete": False,
        },
        "warnings": [],
        "threads": [],
        "turns": [],
        "usage": [],
        "tools": [],
        "edges": [],
        "projects": [],
        "goals": [],
        "environment": {},
        "quota": None,
        "diagnostics": {},
    }


def _resolve_home(home: str | os.PathLike[str] | None) -> Path:
    if home is None:
        value = os.environ.get("CODEX_HOME")
        return Path(value).expanduser().resolve() if value else (Path.home() / ".codex").resolve()
    return Path(home).expanduser().resolve()


def _walk_names(value: Any, prefix: str = "") -> tuple[list[str], list[str], str | None]:
    """只收集配置键路径、MCP/插件名和 sqlite_home，不返回配置值。"""
    fields: list[str] = []
    names: list[str] = []
    sqlite_home: str | None = None
    if not isinstance(value, dict):
        return fields, names, sqlite_home
    for key, child in value.items():
        key_text = _safe_text(key, 120)
        if not key_text:
            continue
        path = f"{prefix}.{key_text}" if prefix else key_text
        fields.append(path)
        lowered = key_text.lower()
        prefix_lower = prefix.lower()
        if lowered == "sqlite_home" and isinstance(child, str):
            sqlite_home = child
        if lowered in {"mcp", "mcp_servers", "mcpservers", "plugins", "plugin"}:
            if isinstance(child, dict):
                # _safe_text 纯函数，赋值表达式只算一次并顺带过滤 None 项。
                names.extend(text for name in child.keys() if (text := _safe_text(name, 120)))
        if any(token in prefix_lower for token in ("mcp", "plugin")) and isinstance(child, dict):
            names.append(key_text)
        child_fields, child_names, child_sqlite = _walk_names(child, path)
        fields.extend(child_fields)
        names.extend(child_names)
        sqlite_home = sqlite_home or child_sqlite
    return fields, names, sqlite_home


def _read_config_metadata(home: Path) -> tuple[dict[str, Any], str | None]:
    files: list[str] = []
    fields: list[str] = []
    names: list[str] = []
    sqlite_home: str | None = None
    for path in (home / "config.toml", home / "config.json", home / "settings.toml"):
        if not path.is_file():
            continue
        files.append(path.name)
        try:
            if path.suffix.lower() == ".toml" and tomllib is not None:
                with path.open("rb") as handle:
                    parsed = tomllib.load(handle)
            else:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            new_fields, new_names, found_sqlite = _walk_names(parsed)
            fields.extend(new_fields)
            names.extend(new_names)
            sqlite_home = sqlite_home or found_sqlite
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError if tomllib else ValueError):
            # 配置字段只是能力提示，解析失败不能让整个平台变成空白。
            continue
    skill_names: list[str] = []
    for directory in (home / "skills", home / "skill"):
        if directory.is_dir():
            try:
                skill_names.extend(sorted(item.name for item in directory.iterdir()))
            except OSError:
                pass
    plugin_names: list[str] = []
    for directory in (home / "plugins", home / "plugin"):
        if directory.is_dir():
            try:
                plugin_names.extend(sorted(item.name for item in directory.iterdir()))
            except OSError:
                pass
    return (
        {
            "config_files": sorted(set(files)),
            "config_fields": sorted(set(fields)),
            "skills_files": sorted(set(skill_names)),
            "plugins": sorted(set(plugin_names + names)),
            "mcp_names": sorted(set(names)),
        },
        sqlite_home,
    )


def _resolve_sqlite_home(home: Path, config_sqlite_home: str | None) -> Path:
    """环境变量优先，其次是非秘密配置字段，最后回到 CODEX_HOME。"""
    env_home = os.environ.get("CODEX_SQLITE_HOME")
    value = env_home or config_sqlite_home
    if not value:
        return home
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = home / path
    return path.resolve()


def _find_sqlite(sqlite_home: Path, home: Path, names: Iterable[str]) -> Path | None:
    candidates: list[Path] = []
    for name in names:
        for base in (sqlite_home, sqlite_home / "db", home, home / "db"):
            candidates.append(base / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    # 只在已确定的 sqlite 目录下浅层寻找，避免扫描 Codex 源目录的大量日志。
    if sqlite_home.is_dir():
        wanted = set(names)
        try:
            for root, directories, files in os.walk(sqlite_home):
                depth = len(Path(root).relative_to(sqlite_home).parts)
                directories[:] = [item for item in directories if not item.startswith(".")]
                if depth > 3:
                    directories[:] = []
                    continue
                for filename in files:
                    if filename in wanted:
                        return (Path(root) / filename).resolve()
        except OSError:
            return None
    return None


def _db_candidates(sqlite_home: Path, home: Path) -> dict[str, Path | None]:
    return {
        "state": _find_sqlite(sqlite_home, home, ("state_5.sqlite", "state.sqlite")),
        "history": _find_sqlite(sqlite_home, home, ("thread_history_1.sqlite", "thread_history.sqlite")),
        "goals": _find_sqlite(sqlite_home, home, ("goals_1.sqlite", "goals.sqlite")),
        "logs": _find_sqlite(sqlite_home, home, ("logs_2.sqlite", "logs.sqlite")),
        "memories": _find_sqlite(sqlite_home, home, ("memories_1.sqlite", "memories.sqlite")),
        "queue": _find_sqlite(sqlite_home, home, ("queue.sqlite", "queue_1.sqlite")),
    }


@contextlib.contextmanager
def _open_ro(path: Path) -> Iterator[sqlite3.Connection]:
    """以 SQLite URI 只读打开，并开启 query_only 和短 busy timeout。"""
    uri = f"file:{quote(str(path), safe='/\\:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.35)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        # Codex 侧库是宿主应用的热库，这里只是只读旁路：锁竞争时快速让路
        # （350ms，与 connect timeout 同值）而不是阻塞刷新管线；个别表读空
        # 由 _select_rows 兜底，下一轮刷新自然补齐。
        connection.execute("PRAGMA busy_timeout=350")
        yield connection
    finally:
        connection.close()


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def _select_rows(connection: sqlite3.Connection, table: str, columns: Iterable[str]) -> list[sqlite3.Row]:
    available = _table_columns(connection, table)
    selected = [column for column in columns if column in available]
    if not selected:
        return []
    quoted = ", ".join(f'"{column}"' for column in selected)
    try:
        return connection.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
    except sqlite3.Error:
        return []


def _row_value(row: sqlite3.Row | dict[str, Any], name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError):
        return default


def _safe_error_type(value: Any) -> str | None:
    parsed = _json_dict(value)
    if parsed is not None:
        for key in ("error_type", "type", "code", "name"):
            candidate = _safe_text(parsed.get(key), 120)
            if candidate:
                return candidate
        return None
    text = _safe_text(value, 120)
    return text


def _nested_parent_id(value: Any, depth: int = 0) -> str | None:
    """读取 subagent.thread_spawn 的父线程 ID，不展开其它 source 内容。"""
    if depth > 4 or not isinstance(value, dict):
        return None
    for key in ("parent_thread_id", "parent_id"):
        found = _as_id(value.get(key))
        if found:
            return found
    for child in value.values():
        found = _nested_parent_id(child, depth + 1)
        if found:
            return found
    return None


def _read_state(path: Path | None, warnings: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "threads": [], "projects": [], "edges": []}
    if path is None:
        return result
    try:
        with _open_ro(path) as connection:
            thread_rows = _select_rows(
                connection,
                "threads",
                (
                    "id", "rollout_path", "created_at", "updated_at", "created_at_ms", "updated_at_ms",
                    "source", "model_provider", "cwd", "title", "tokens_used", "git_sha", "git_branch",
                    "cli_version", "agent_role", "model", "reasoning_effort", "project_id", "archived", "status",
                ),
            )
            for row in thread_rows:
                thread_id = _as_id(_row_value(row, "id"))
                if not thread_id:
                    continue
                source = _json_dict(_row_value(row, "source")) or {}
                parent_id = _nested_parent_id(source)
                created = _timestamp_ms(_row_value(row, "created_at_ms")) or _timestamp_ms(_row_value(row, "created_at"))
                updated = _timestamp_ms(_row_value(row, "updated_at_ms")) or _timestamp_ms(_row_value(row, "updated_at"))
                archived = _as_bool(_row_value(row, "archived"))
                status = _safe_text(_row_value(row, "status"), 80) or ("archived" if archived else "unknown")
                result["threads"].append(
                    {
                        "id": thread_id,
                        "title": _safe_text(_row_value(row, "title"), 240) or "未命名任务",
                        "cwd": _safe_text(_row_value(row, "cwd"), 600),
                        "project_id": _as_id(_row_value(row, "project_id")),
                        "project_name": None,
                        "project_inferred": False,
                        "created_at": created,
                        "updated_at": updated,
                        "model": _safe_text(_row_value(row, "model"), 160),
                        "provider": _safe_text(_row_value(row, "model_provider"), 160),
                        "role": _safe_text(_row_value(row, "agent_role"), 120),
                        "reasoning_effort": _safe_text(_row_value(row, "reasoning_effort"), 80),
                        "parent_id": parent_id,
                        "archived": archived,
                        "git_branch": _safe_text(_row_value(row, "git_branch"), 240),
                        "git_sha": _safe_text(_row_value(row, "git_sha"), 160),
                        "cli_version": _safe_text(_row_value(row, "cli_version"), 120),
                        "recorded_tokens": _as_int(_row_value(row, "tokens_used"), 0),
                        "status": status,
                        "last_activity_at": updated or created,
                        "rollout_path": _safe_text(_row_value(row, "rollout_path"), 1200),
                    }
                )
            project_rows = _select_rows(connection, "projects", ("id", "name"))
            projects: dict[str, dict[str, Any]] = {}
            for row in project_rows:
                project_id = _as_id(_row_value(row, "id"))
                if project_id:
                    projects[project_id] = {"id": project_id, "name": _safe_text(_row_value(row, "name"), 240), "roots": []}
            root_rows = _select_rows(connection, "project_roots", ("project_id", "path"))
            for row in root_rows:
                project_id = _as_id(_row_value(row, "project_id"))
                root = _safe_text(_row_value(row, "path"), 1200)
                if project_id and root and project_id in projects and root not in projects[project_id]["roots"]:
                    projects[project_id]["roots"].append(root)
            result["projects"] = list(projects.values())
            edge_rows = _select_rows(connection, "thread_spawn_edges", ("parent_thread_id", "child_thread_id", "status"))
            for row in edge_rows:
                parent_id = _as_id(_row_value(row, "parent_thread_id"))
                child_id = _as_id(_row_value(row, "child_thread_id"))
                if parent_id and child_id:
                    result["edges"].append(
                        {"parent_id": parent_id, "child_id": child_id, "status": _safe_text(_row_value(row, "status"), 80)}
                    )
            thread_by_id = {item.get("id"): item for item in result["threads"]}
            for edge in result["edges"]:
                child = thread_by_id.get(edge.get("child_id"))
                if child and not child.get("parent_id"):
                    child["parent_id"] = edge.get("parent_id")
            # 旧状态库可能没有独立 edge 表；source 中已明确的父子关系仍可
            # 作为关系记录输出，但 status 不推断为“正在运行”。
            known_edges = {(item["parent_id"], item["child_id"]) for item in result["edges"]}
            for thread in result["threads"]:
                parent_id = thread.get("parent_id")
                child_id = thread.get("id")
                if parent_id and child_id and (parent_id, child_id) not in known_edges:
                    result["edges"].append({"parent_id": parent_id, "child_id": child_id, "status": "recorded"})
            result["ok"] = True
    except (OSError, sqlite3.Error) as exc:
        warnings.append(f"Codex 状态库读取失败：{_safe_text(exc, 160) or '未知错误'}")
    return result


def _read_history(path: Path | None, warnings: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "turns": [], "tools": []}
    if path is None:
        return result
    try:
        with _open_ro(path) as connection:
            turn_rows = _select_rows(
                connection,
                "thread_turns",
                ("thread_id", "turn_id", "status", "error_json", "started_at", "completed_at", "duration_ms"),
            )
            for row in turn_rows:
                thread_id = _as_id(_row_value(row, "thread_id"))
                turn_id = _as_id(_row_value(row, "turn_id"))
                if not thread_id or not turn_id:
                    continue
                result["turns"].append(
                    {
                        "id": turn_id,
                        "thread_id": thread_id,
                        "status": _safe_text(_row_value(row, "status"), 80),
                        "started_at": _timestamp_ms(_row_value(row, "started_at")),
                        "completed_at": _timestamp_ms(_row_value(row, "completed_at")),
                        "duration_ms": _as_int(_row_value(row, "duration_ms")),
                        "error_type": _safe_error_type(_row_value(row, "error_json")),
                    }
                )
            columns = _table_columns(connection, "thread_items")
            if "item_json" in columns:
                expressions = [
                    '"thread_id"', '"turn_id"', '"item_id"', '"created_at_ms"', '"item_type"',
                    "CASE WHEN json_valid(\"item_json\") THEN json_extract(\"item_json\", '$.type') END AS json_type",
                    "CASE WHEN json_valid(\"item_json\") THEN json_extract(\"item_json\", '$.status') END AS json_status",
                    "CASE WHEN json_valid(\"item_json\") THEN json_extract(\"item_json\", '$.durationMs') END AS json_duration",
                    "CASE WHEN json_valid(\"item_json\") THEN json_extract(\"item_json\", '$.exitCode') END AS json_exit_code",
                    "CASE WHEN json_valid(\"item_json\") THEN json_extract(\"item_json\", '$.tool') END AS json_tool",
                    "CASE WHEN json_valid(\"item_json\") THEN json_extract(\"item_json\", '$.name') END AS json_name",
                    "CASE WHEN json_valid(\"item_json\") THEN json_extract(\"item_json\", '$.server') END AS json_server",
                ]
                available = {expr.strip('"') for expr in columns}
                base = [expr for expr in expressions[:5] if expr.strip('"') in available]
                if base:
                    try:
                        rows = connection.execute(
                            f'SELECT {", ".join(base + expressions[5:])} FROM "thread_items"'
                        ).fetchall()
                    except sqlite3.Error:
                        rows = []
                    for row in rows:
                        item_type = _safe_text(_row_value(row, "item_type"), 80) or _safe_text(_row_value(row, "json_type"), 80)
                        if item_type not in _TOOL_TYPES:
                            continue
                        thread_id = _as_id(_row_value(row, "thread_id"))
                        turn_id = _as_id(_row_value(row, "turn_id"))
                        item_id = _as_id(_row_value(row, "item_id"))
                        if not thread_id or not item_id:
                            continue
                        tool_name = _row_value(row, "json_name") or _row_value(row, "json_tool")
                        # json_tool 有时是包含参数的对象；工具参数/结果属于
                        # 正文边界，只接受短标量名称，绝不把对象转成字符串导出。
                        if not isinstance(tool_name, (str, int, float)):
                            tool_name = None
                        server_name = _row_value(row, "json_server")
                        if not isinstance(server_name, (str, int, float)):
                            server_name = None
                        result["tools"].append(
                            {
                                "id": item_id,
                                "thread_id": thread_id,
                                "turn_id": turn_id,
                                "type": item_type,
                                "name": _safe_text(tool_name, 160),
                                "status": _safe_text(_row_value(row, "json_status"), 80),
                                "duration_ms": _as_int(_row_value(row, "json_duration")),
                                "timestamp": _timestamp_ms(_row_value(row, "created_at_ms")),
                                "exit_code": _as_int(_row_value(row, "json_exit_code")),
                                "server": _safe_text(server_name, 160),
                            }
                        )
            result["ok"] = True
    except (OSError, sqlite3.Error) as exc:
        warnings.append(f"Codex 历史库读取失败：{_safe_text(exc, 160) or '未知错误'}")
    return result


def _read_goals(path: Path | None, warnings: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "goals": []}
    if path is None:
        return result
    try:
        with _open_ro(path) as connection:
            rows = _select_rows(
                connection,
                "thread_goals",
                ("thread_id", "goal_id", "objective", "status", "token_budget", "tokens_used", "time_used_seconds"),
            )
            for row in rows:
                thread_id = _as_id(_row_value(row, "thread_id"))
                goal_id = _as_id(_row_value(row, "goal_id"))
                if thread_id and goal_id:
                    result["goals"].append(
                        {
                            "id": goal_id,
                            "thread_id": thread_id,
                            "objective": _safe_text(_row_value(row, "objective"), 120),
                            "status": _safe_text(_row_value(row, "status"), 80),
                            "token_budget": _as_int(_row_value(row, "token_budget")),
                            "tokens_used": _as_int(_row_value(row, "tokens_used")),
                            "time_used_seconds": _as_int(_row_value(row, "time_used_seconds")),
                        }
                    )
            result["ok"] = True
    except (OSError, sqlite3.Error) as exc:
        warnings.append(f"Codex 目标库读取失败：{_safe_text(exc, 160) or '未知错误'}")
    return result


def _read_count_metadata(path: Path | None, allow: set[str]) -> dict[str, int] | None:
    if path is None:
        return None
    counts: dict[str, int] = {}
    try:
        with _open_ro(path) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for row in rows:
                name = str(row[0])
                if name not in allow:
                    continue
                try:
                    counts[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                except sqlite3.Error:
                    continue
    except (OSError, sqlite3.Error):
        return None
    return counts


def _read_log_diagnostics(path: Path | None) -> dict[str, int]:
    """只按 level/target 聚合诊断日志，不读取 feedback_log_body 等正文。"""
    counts: dict[str, int] = {}
    if path is None:
        return counts
    try:
        with _open_ro(path) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for row in tables:
                table = str(row[0])
                columns = _table_columns(connection, table)
                if "level" not in columns and "target" not in columns:
                    continue
                selected = [name for name in ("level", "target") if name in columns]
                if not selected:
                    continue
                quoted = ", ".join(f'"{name}"' for name in selected)
                try:
                    for item in connection.execute(f'SELECT {quoted} FROM "{table}"'):
                        level = _safe_text(item["level"], 80) if "level" in selected else "unknown"
                        target = _safe_text(item["target"], 120) if "target" in selected else "unknown"
                        key = f"{level or 'unknown'}:{target or 'unknown'}"
                        counts[key] = counts.get(key, 0) + 1
                except sqlite3.Error:
                    continue
    except (OSError, sqlite3.Error):
        return counts
    return counts


def _read_environment(home: Path, sqlite_home: Path, config: dict[str, Any], dbs: dict[str, Path | None]) -> dict[str, Any]:
    memories = _read_count_metadata(dbs.get("memories"), {"jobs", "stage1_outputs"})
    queue = _read_count_metadata(dbs.get("queue"), set())
    return {
        "codex_home": str(home),
        "sqlite_home": str(sqlite_home),
        "config_files": config.get("config_files", []),
        "config_fields": config.get("config_fields", []),
        "skills_files": config.get("skills_files", []),
        "plugins": config.get("plugins", []),
        "mcp_names": config.get("mcp_names", []),
        "automations": {"available": False, "reason": "unavailable"},
        "memories": {"available": memories is not None, "counts": memories or {}},
        "queue": {"available": queue is not None, "counts": queue or {}},
    }


def _normal_path(value: Any) -> str | None:
    text = _safe_text(value, 1200)
    if not text:
        return None
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(text))).rstrip("\\/")
    except (OSError, ValueError):
        return text.lower().rstrip("\\/")


def _associate_projects(threads: list[dict[str, Any]], projects: list[dict[str, Any]]) -> None:
    by_id = {str(project.get("id")): project for project in projects if project.get("id") is not None}
    roots: list[tuple[str, str]] = []
    for project in projects:
        project_id = _as_id(project.get("id"))
        if not project_id:
            continue
        for root in project.get("roots", []):
            normalized = _normal_path(root)
            if normalized:
                roots.append((normalized, project_id))
    roots.sort(key=lambda pair: len(pair[0]), reverse=True)
    for thread in threads:
        project_id = _as_id(thread.get("project_id"))
        if project_id in by_id:
            thread["project_name"] = by_id[project_id].get("name")
            continue
        cwd = _normal_path(thread.get("cwd"))
        if not cwd:
            continue
        for root, candidate_id in roots:
            if cwd == root or cwd.startswith(root + os.sep):
                thread["project_id"] = candidate_id
                thread["project_name"] = by_id[candidate_id].get("name")
                thread["project_inferred"] = True
                break


def _resolve_rollout_path(value: Any, home: Path) -> Path | None:
    text = _safe_text(value, 1600)
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = home / path
    return path.resolve()


def _fingerprint(path: Path) -> str | None:
    try:
        stat = path.stat()
        with path.open("rb") as handle:
            first = handle.read(4096)
            # 指纹只依赖稳定的前缀；把文件大小/尾部放进指纹会让正常追加
            # 被误判为替换，进而每次从零重建大日志。缩短由 offset>size
            # 检测，等长替换通常会改变前缀并触发重建。
        digest = hashlib.sha256()
        digest.update(first)
        return digest.hexdigest()
    except OSError:
        return None


def _content_fingerprint(path: Path) -> str | None:
    """在文件大小不变时用于识别原地替换的前后采样指纹。"""
    try:
        stat = path.stat()
        with path.open("rb") as handle:
            first = handle.read(4096)
            if stat.st_size > 4096:
                handle.seek(max(0, stat.st_size - 4096))
                last = handle.read(4096)
            else:
                last = first
        digest = hashlib.sha256()
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(first)
        digest.update(last)
        return digest.hexdigest()
    except OSError:
        return None


def _default_cache_dir(home: Path) -> Path:
    configured = os.environ.get("GAUGE_DATA_DIR")
    if configured:
        path = Path(configured).expanduser()
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        path = Path(local_app_data).expanduser() / "GAUGE" / "cache" if local_app_data else Path(tempfile.gettempdir()) / "GAUGE" / "cache"
    try:
        if path.resolve() == home or home in path.resolve().parents:
            path = Path(tempfile.gettempdir()) / "GAUGE" / "cache"
    except OSError:
        pass
    return path.resolve()


def _cache_paths(home: Path, cache_dir: str | os.PathLike[str] | None) -> tuple[Path, Path]:
    directory = Path(cache_dir).expanduser().resolve() if cache_dir is not None else _default_cache_dir(home)
    try:
        if directory == home or home in directory.parents:
            directory = _default_cache_dir(home)
    except OSError:
        directory = _default_cache_dir(home)
    key = hashlib.sha256(str(home).encode("utf-8", "replace")).hexdigest()[:20]
    return directory, directory / f"{_CACHE_NAME_PREFIX}{key}.json"


def _blank_cache(home: Path) -> dict[str, Any]:
    return {"schema_version": _CACHE_VERSION, "source_home": str(home), "files": {}, "last_snapshot": None}


def _load_cache(path: Path, home: Path, warnings: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return _blank_cache(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != _CACHE_VERSION or data.get("source_home") != str(home):
            return _blank_cache(home)
        if not isinstance(data.get("files"), dict):
            raise ValueError("files 不是对象")
        # 旧版本曾把半行原文 base64 写入 partial；读到后立即丢弃，
        # offset 会让下一轮从源文件重读，不把历史正文继续带入缓存。
        for state in data["files"].values():
            if isinstance(state, dict):
                state.pop("partial", None)
                state["partial_bytes"] = _as_int(state.get("partial_bytes"), 0) or 0
        return data
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        warnings.append(f"Codex 增量缓存损坏，已重建：{_safe_text(exc, 120) or '未知错误'}")
    return _blank_cache(home)


def _merge_cache_states(current: dict[str, Any], disk: dict[str, Any]) -> dict[str, Any]:
    """在写锁内合并并发刷新结果，避免较旧读快照覆盖已推进的 offset。"""
    merged = dict(current)
    # 先取出再守卫：对两次独立的 .get 结果做 isinstance 无法收窄第一次的值。
    current_raw = current.get("files")
    disk_raw = disk.get("files")
    current_files = current_raw if isinstance(current_raw, dict) else {}
    disk_files = disk_raw if isinstance(disk_raw, dict) else {}
    files: dict[str, Any] = {}
    for key in set(current_files) | set(disk_files):
        left = current_files.get(key)
        right = disk_files.get(key)
        if not isinstance(left, dict):
            files[key] = right
            continue
        if not isinstance(right, dict):
            files[key] = left
            continue
        # 指纹/大小变化代表截断或替换，当前调用者的重建状态优先；
        # 同一文件则保留 offset 更大的状态。
        left_size = _as_int(left.get("size"), 0) or 0
        right_size = _as_int(right.get("size"), 0) or 0
        left_prefix = left.get("fingerprint")
        right_prefix = right.get("fingerprint")
        if left_prefix and right_prefix and left_prefix != right_prefix:
            chosen = left
        elif left_size < right_size and (_as_int(left.get("offset"), 0) or 0) == 0:
            chosen = left
        else:
            left_offset = _as_int(left.get("offset"), 0) or 0
            right_offset = _as_int(right.get("offset"), 0) or 0
            chosen = left if left_offset >= right_offset else right
        files[key] = chosen
    merged["files"] = files
    left_snapshot = current.get("last_snapshot")
    right_snapshot = disk.get("last_snapshot")
    # 默认值已是 0，_as_int 不会返回 None；or 0 仅用于收窄比较类型。
    left_time = (_as_int(left_snapshot.get("generated_at_ms"), 0) or 0) if isinstance(left_snapshot, dict) else 0
    right_time = (_as_int(right_snapshot.get("generated_at_ms"), 0) or 0) if isinstance(right_snapshot, dict) else 0
    if right_time > left_time:
        merged["last_snapshot"] = right_snapshot
    return merged


@contextlib.contextmanager
def _cache_file_lock(lock_path: Path) -> Iterator[bool]:
    acquired = False
    handle = None
    deadline = time.monotonic() + _CACHE_LOCK_TIMEOUT
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        while time.monotonic() < deadline:
            try:
                handle = lock_path.open("x", encoding="ascii")
                handle.write(str(os.getpid()))
                handle.flush()
                acquired = True
                break
            except FileExistsError:
                time.sleep(0.02)
            except OSError:
                break
        yield acquired
    finally:
        if handle is not None:
            handle.close()
        if acquired:
            try:
                lock_path.unlink()
            except OSError:
                pass


def _write_cache_payload(path: Path, cache: dict[str, Any], warnings: list[str]) -> bool:
    """在调用者已持有文件锁时原子写入缓存。"""
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 读取当前磁盘版本也在同一互斥区内；两个刷新进程即使
        # 各自从旧快照开始，也不会用旧 offset 覆盖较新的推进。
        if path.is_file():
            try:
                on_disk = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(on_disk, dict) and on_disk.get("schema_version") == _CACHE_VERSION:
                    cache = _merge_cache_states(cache, on_disk)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".codex-cache-", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(cache, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        warnings.append(f"Codex 增量缓存写入失败：{_safe_text(exc, 140) or '未知错误'}")
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        return False


def _write_cache(path: Path, cache: dict[str, Any], warnings: list[str], *, lock_held: bool = False) -> bool:
    lock_path = path.with_suffix(path.suffix + ".lock")
    if lock_held:
        return _write_cache_payload(path, cache, warnings)
    with _CACHE_LOCK:
        with _cache_file_lock(lock_path) as acquired:
            if not acquired:
                warnings.append("Codex 增量缓存锁定超时，本次不写缓存")
                return False
            return _write_cache_payload(path, cache, warnings)


def _event_ms(event: dict[str, Any], payload: dict[str, Any] | None = None) -> int | None:
    payload = payload or {}
    for key in ("timestamp_ms", "created_at_ms", "timestamp", "created_at", "time"):
        found = _timestamp_ms(event.get(key))
        if found is not None:
            return found
    for key in ("timestamp_ms", "created_at_ms", "timestamp", "created_at", "time"):
        found = _timestamp_ms(payload.get(key))
        if found is not None:
            return found
    return None


def _usage_vector(value: Any) -> dict[str, int | None] | None:
    if not isinstance(value, dict):
        return None
    aliases = {
        "input_tokens": ("input_tokens", "input"),
        "cached_input_tokens": ("cached_input_tokens", "cache_read_input_tokens", "cached_input"),
        # cache_write_input 实测当前全 0，但口径 1.1 冻结了该字段，不得静默丢。
        "cache_write_input": ("cache_write_input_tokens", "cache_write_input"),
        "output_tokens": ("output_tokens", "output"),
        "reasoning_output_tokens": ("reasoning_output_tokens", "reasoning_output"),
        "total_tokens": ("total_tokens", "total"),
    }
    vector: dict[str, int | None] = {}
    found = False
    for target, candidates in aliases.items():
        current = None
        for candidate in candidates:
            if candidate in value:
                current = _as_int(value.get(candidate))
                found = found or current is not None
                break
        vector[target] = current
    return vector if found else None


def _minimal_usage_record(
    event: dict[str, Any],
    payload: dict[str, Any],
    source_thread_id: str | None,
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    context = context or {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None, None
    response_id = _as_id(payload.get("response_id") or event.get("response_id"))
    # token_usage_record 的所有权只认 payload.thread_id；不能把 fork 日志
    # 当前文件所属线程误当成 token 所有者。
    owner_thread_id = _as_id(payload.get("thread_id"))
    turn_id = _as_id(payload.get("turn_id") or event.get("turn_id") or context.get("turn_id"))
    vector = _usage_vector(usage)
    if not response_id:
        return None, None, "跳过缺少 response_id 的 usage_record"
    if vector is None or not owner_thread_id:
        return None, None, "跳过缺少 payload.thread_id 或 usage 的 usage_record"
    input_tokens = vector.get("input_tokens")
    output_tokens = vector.get("output_tokens")
    reasoning_tokens = vector.get("reasoning_output_tokens")
    cached_tokens = vector.get("cached_input_tokens")
    cache_write_tokens = vector.get("cache_write_input")
    total_reported = vector.get("total_tokens")
    total = (input_tokens or 0) + (output_tokens or 0)
    warning = None
    if total_reported is not None and total_reported != total:
        warning = f"usage_record total_tokens 与 input+output 不一致：{total_reported} != {total}"
    timestamp = _event_ms(event, payload)
    record = {
        "id": response_id,
        "response_id": response_id,
        "thread_id": owner_thread_id,
        "source_thread_id": source_thread_id,
        "turn_id": turn_id,
        "root_turn_id": _as_id(payload.get("root_turn_id")),
        "timestamp": timestamp,
        "model": _safe_text(payload.get("model") or event.get("model") or context.get("model"), 160),
        "provider": _safe_text(payload.get("provider") or event.get("provider") or context.get("provider"), 160),
        "input_tokens": input_tokens if input_tokens is not None else 0,
        "cached_input_tokens": cached_tokens if cached_tokens is not None else 0,
        "cache_write_input": cache_write_tokens if cache_write_tokens is not None else 0,
        "output_tokens": output_tokens if output_tokens is not None else 0,
        "reasoning_output_tokens": reasoning_tokens if reasoning_tokens is not None else 0,
        "total_tokens": total,
        "derived": False,
    }
    return record, None, warning


def _quota_from_value(value: Any, captured_at: int | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    primary = value.get("primary") or value.get("five_hour") or value.get("fiveHour")
    secondary = value.get("secondary") or value.get("weekly") or value.get("week")
    if not isinstance(primary, dict) and not isinstance(secondary, dict):
        return None

    def window(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        used = item.get("used_percent", item.get("usedPercent"))
        minutes = item.get("window_minutes", item.get("windowDurationMins", item.get("window_minutes")))
        reset = item.get("resets_at", item.get("resetsAt"))
        if used is None and minutes is None and reset is None:
            return None
        try:
            used_value = float(used) if used is not None else None
        except (TypeError, ValueError):
            used_value = None
        # 契约冻结形状：resets_at 源为 epoch 秒，统一输出毫秒；
        # _timestamp_ms 的 1e11 阈值兜住秒/毫秒歧义。
        return {
            "used_percent": used_value,
            "window_minutes": _as_int(minutes),
            "resets_at_ms": _timestamp_ms(reset),
        }

    primary_window = window(primary)
    secondary_window = window(secondary)
    if primary_window is None and secondary_window is None:
        # limit_id=premium 等变体（primary/secondary 均为 null）在这里整体
        # 返回 None，由调用方跳过，不产出半残快照。
        return None
    credits = value.get("credits")
    credits = credits if isinstance(credits, dict) else {}
    has_credits = credits.get("has_credits")
    if has_credits is None:
        # has_ccredits 为历史拼写变体，一并容忍；缺失按 False 处理。
        has_credits = credits.get("has_ccredits")
    return {
        "captured_at": captured_at,
        "source": "rollout_snapshot",
        "plan_type": _safe_text(value.get("plan_type", value.get("planType")), 80),
        "primary": primary_window,
        "secondary": secondary_window,
        "credits": {
            "has_credits": _as_bool(has_credits),
            "unlimited": _as_bool(credits.get("unlimited")),
            "balance": _safe_text(credits.get("balance"), 80),
        },
    }


def _find_quota(value: Any, captured_at: int | None, depth: int = 0) -> dict[str, Any] | None:
    if depth > 4 or not isinstance(value, dict):
        return None
    for key in ("quota", "rate_limits", "rateLimits", "rate_limits_by_limit_id", "rateLimitsByLimitId"):
        found = _quota_from_value(value.get(key), captured_at)
        if found:
            return found
    direct = _quota_from_value(value, captured_at)
    if direct:
        return direct
    for child in value.values():
        found = _find_quota(child, captured_at, depth + 1)
        if found:
            return found
    return None


def _find_scalar(value: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    """在 session_meta/turn_context 的有限深度内找一个标量上下文字段。"""
    if depth > 3 or not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, (str, int, float)) and not isinstance(candidate, bool):
            return candidate
    for child in value.values():
        found = _find_scalar(child, keys, depth + 1)
        if found is not None:
            return found
    return None


def _update_rollout_context(
    event: dict[str, Any], payload: dict[str, Any], context: dict[str, Any]
) -> None:
    """按日志顺序保存当前 turn/model/provider 的短上下文，不保存事件正文。"""
    for target, keys in (
        ("turn_id", ("turn_id", "turnId")),
        ("model", ("model", "model_id", "modelId")),
        ("provider", ("provider", "model_provider", "modelProvider", "provider_id", "providerId")),
    ):
        found = _find_scalar(payload, keys) or _find_scalar(event, keys)
        if found is not None:
            context[target] = _safe_text(found, 160)


def _parse_rollout_line(
    raw: bytes,
    source_thread_id: str | None,
    source_key: str,
    line_no: int,
    warnings: list[str],
    context: dict[str, Any],
    modern: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    try:
        event = json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, None, None
    if not isinstance(event, dict):
        return None, None, None
    payload = _json_dict(event.get("payload")) or {}
    _update_rollout_context(event, payload, context)
    top_type = _safe_text(event.get("type"), 100)
    if top_type == "token_usage_record" or payload.get("type") == "token_usage_record":
        record, _unused, warning = _minimal_usage_record(event, payload, source_thread_id, context)
        if warning:
            warnings.append(warning)
        return record, None, _find_quota(event, _event_ms(event, payload))
    # 旧格式 token_count 没有 usage_record；契约 1.2 规则 3 裁定单次响应
    # 用量取 info.last_token_usage（与 usage_record 等价，同一数据两种投影），
    # 逐事件直接取值即可，不需要对累计 total_token_usage 做增量推算——
    # 历史累计值存在重复、重置与跨 fork 继承，增量无法安全归属。
    if payload.get("type") == "token_count" or top_type == "token_count":
        info = payload.get("info") if isinstance(payload.get("info"), dict) else event.get("info")
        info = info if isinstance(info, dict) else {}
        vector = _usage_vector(info.get("total_token_usage"))
        if vector is None:
            vector = _usage_vector(info.get("last_token_usage"))
        snapshot = None
        owner = _as_id(payload.get("thread_id")) or source_thread_id
        if vector is not None and owner:
            snapshot = {
                "thread_id": owner,
                "turn_id": _as_id(payload.get("turn_id") or event.get("turn_id")),
                "timestamp": _event_ms(event, payload),
                "model": _safe_text(payload.get("model") or event.get("model") or context.get("model"), 160),
                "provider": _safe_text(payload.get("provider") or event.get("provider") or context.get("provider"), 160),
                "vector": vector,
                "position": line_no,
            }
        record = None
        # 文件级现代格式门：token_usage_record 与 token_count 实测并存于同一
        # 文件，现代文件的 per-response 用量唯一来源是 record；再从 token_count
        # 派生会让同一响应计两次。quota 与覆盖快照的提取不受门影响。
        if not modern:
            last_vector = _usage_vector(info.get("last_token_usage"))
            if last_vector is not None and owner:
                input_tokens = last_vector.get("input_tokens")
                output_tokens = last_vector.get("output_tokens")
                cached_tokens = last_vector.get("cached_input_tokens")
                cache_write_tokens = last_vector.get("cache_write_input")
                reasoning_tokens = last_vector.get("reasoning_output_tokens")
                # 合成 id 只需在同一文件内按行区分；sha1(source_key) 前缀保证跨
                # 文件不碰撞，同一行重复解析得到同一 id，可参与 response_id 去重。
                digest = hashlib.sha1(source_key.encode("utf-8", "replace")).hexdigest()[:8]
                synthetic_id = f"legacy-{digest}-{line_no}"
                record = {
                    "id": synthetic_id,
                    "response_id": synthetic_id,
                    "thread_id": owner,
                    "source_thread_id": source_thread_id,
                    "turn_id": _as_id(payload.get("turn_id") or event.get("turn_id") or context.get("turn_id")),
                    "root_turn_id": _as_id(payload.get("root_turn_id")),
                    "timestamp": _event_ms(event, payload),
                    "model": _safe_text(payload.get("model") or event.get("model") or context.get("model"), 160),
                    "provider": _safe_text(payload.get("provider") or event.get("provider") or context.get("provider"), 160),
                    "input_tokens": input_tokens if input_tokens is not None else 0,
                    "cached_input_tokens": cached_tokens if cached_tokens is not None else 0,
                    "cache_write_input": cache_write_tokens if cache_write_tokens is not None else 0,
                    "output_tokens": output_tokens if output_tokens is not None else 0,
                    "reasoning_output_tokens": reasoning_tokens if reasoning_tokens is not None else 0,
                    "total_tokens": (input_tokens or 0) + (output_tokens or 0),
                    "derived": True,
                }
        return record, snapshot, _find_quota(event, _event_ms(event, payload))
    return None, None, _find_quota(event, _event_ms(event, payload))


def _process_rollout_files(
    files: dict[str, dict[str, Any]],
    rollout_sources: list[tuple[Path, str | None]],
    max_bytes: int,
    max_seconds: float,
    warnings: list[str],
) -> tuple[int, int, int, bool]:
    """按增量 offset 读取 rollout，返回 processed、total、pending、complete。"""
    started = time.monotonic()
    budget = max(0, int(max_bytes))
    consumed = 0
    total_files = len(rollout_sources)
    processed_files = 0
    pending_bytes = 0
    # 最近修改的日志先处理，offset 会持续推进，避免大文件永远霸占首轮预算。
    ordered: list[tuple[float, Path, str | None]] = []
    for path, thread_id in rollout_sources:
        try:
            ordered.append((path.stat().st_mtime, path, thread_id))
        except OSError:
            ordered.append((0.0, path, thread_id))
    ordered.sort(key=lambda item: item[0], reverse=True)
    current_keys = {str(path) for _, path, _ in ordered}
    for source_key in list(files):
        if source_key not in current_keys:
            files[source_key]["missing"] = True
    for _mtime, path, source_thread_id in ordered:
        source_key = str(path)
        state = files.setdefault(
            source_key,
            {"offset": 0, "partial_bytes": 0, "fingerprint": None, "content_fingerprint": None, "modern": False, "context": {}, "records": [], "snapshots": [], "quota": []},
        )
        state["missing"] = False
        try:
            stat = path.stat()
            current_size = int(stat.st_size)
            current_fingerprint = _fingerprint(path)
            current_content_fingerprint = _content_fingerprint(path)
        except OSError:
            state["missing"] = True
            warnings.append(f"Codex rollout 文件不可读：{_safe_text(path.name, 160) or '未知文件'}")
            continue
        offset = _as_int(state.get("offset"), 0) or 0
        previous_size = _as_int(state.get("size"), offset) or offset
        prefix_changed = bool(
            offset > 0
            and state.get("fingerprint")
            and current_fingerprint
            and state.get("fingerprint") != current_fingerprint
        )
        same_size_changed = bool(
            offset > 0
            and previous_size == current_size
            and state.get("content_fingerprint")
            and current_content_fingerprint
            and state.get("content_fingerprint") != current_content_fingerprint
        )
        if offset > current_size or prefix_changed or same_size_changed:
            state.clear()
            state.update({"offset": 0, "partial_bytes": 0, "fingerprint": None, "content_fingerprint": None, "modern": False, "context": {}, "records": [], "snapshots": [], "quota": []})
            offset = 0
        state["fingerprint"] = current_fingerprint
        state["content_fingerprint"] = current_content_fingerprint
        state["size"] = current_size
        state["mtime_ns"] = _as_int(getattr(stat, "st_mtime_ns", None), 0)
        if offset >= current_size:
            processed_files += 1
            continue
        if budget <= consumed or time.monotonic() >= started + max(0.01, float(max_seconds)):
            pending_bytes += max(0, current_size - offset)
            continue
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                read_limit = min(max(1, budget - consumed), current_size - offset)
                chunk = handle.read(read_limit)
        except OSError as exc:
            warnings.append(f"Codex rollout 读取失败：{_safe_text(exc, 140) or '未知错误'}")
            pending_bytes += max(0, current_size - offset)
            continue
        consumed += len(chunk)
        # offset 始终停在最后一个完整换行之后，因此下一次从 offset
        # 重读时已经包含上轮的半行；缓存 partial 只用于状态记录，不能
        # 再拼接一次，否则追加换行后会把同一 JSON 行复制成两行。
        combined = chunk
        complete_end = combined.rfind(b"\n") + 1
        complete_bytes = combined[:complete_end] if complete_end else b""
        remainder = combined[complete_end:] if complete_end else combined
        line_no = len(state.get("records", [])) + len(state.get("snapshots", [])) + 1
        context = state.setdefault("context", {})
        processed_bytes = 0
        if complete_bytes:
            # splitlines(keepends=True) 让时间预算只提交已经完整解析的行；
            # 未提交部分保持 offset 不动，下一轮从源文件重读，缓存只记字节数。
            for raw_line in complete_bytes.splitlines(keepends=True):
                if time.monotonic() >= started + max(0.01, float(max_seconds)):
                    break
                record, snapshot, quota = _parse_rollout_line(
                    raw_line.rstrip(b"\r\n"), source_thread_id, source_key, line_no, warnings, context,
                    bool(state.get("modern")),
                )
                line_no += 1
                processed_bytes += len(raw_line)
                if record is not None:
                    state.setdefault("records", []).append(record)
                    if not record.get("derived"):
                        # 现代格式门：本文件出现首个真实 token_usage_record 后，
                        # 后续 token_count 不再派生 usage；标志随缓存状态持久化。
                        state["modern"] = True
                if snapshot is not None:
                    state.setdefault("snapshots", []).append(snapshot)
                if quota is not None:
                    state.setdefault("quota", []).append(quota)
        advanced = processed_bytes
        state["offset"] = offset + advanced
        state["partial_bytes"] = len(combined) - advanced
        if state["offset"] >= current_size and not state["partial_bytes"]:
            processed_files += 1
        else:
            pending_bytes += max(0, current_size - int(state["offset"]))
        # 不 break：后续文件的 pending_bytes 也必须进入 coverage，避免续扫
        # 因时间预算中止时把未触及文件误报成完整。
    # consumed<=budget 恒真（读取量受 read_limit=min(budget-consumed, …) 钳制），
    # 不参与判定；complete 只看文件推进与遗留字节。
    complete = processed_files == total_files and pending_bytes == 0
    return processed_files, total_files, pending_bytes, complete


def _legacy_usage(files: dict[str, dict[str, Any]], warnings: list[str]) -> list[dict[str, Any]]:
    """把旧格式 rollout 的 derived 记录并入 usage 明细。

    契约 1.2 规则 3：旧格式没有 token_usage_record，per-response 用量取
    token_count.info.last_token_usage（实测与 usage_record 等价，同一数据
    两种投影），由解析层逐事件生成 derived 记录，全程不做增量运算；
    累计 total_token_usage 不参与派生，仅以快照形式保留覆盖信息。

    现代文件同时携带 token_count（实测 133/136 同文件并存）时，增量读取
    早期可能在见到首个 token_usage_record 之前误派生 derived 记录；同文件
    只要存在真实 record，一律丢弃 derived，防止每响应双计。
    """
    records: list[dict[str, Any]] = []
    saw_legacy = False
    for state in files.values():
        if state.get("missing"):
            continue
        file_records = [item for item in state.get("records", []) if isinstance(item, dict)]
        has_real = any(not item.get("derived") for item in file_records)
        if has_real:
            file_records = [item for item in file_records if not item.get("derived")]
        else:
            # 有 token_count 快照却没有真实 record 的文件才是旧格式；现代
            # 文件的快照只承载 quota/覆盖信息，不触发旧格式缺字段告警。
            saw_legacy = saw_legacy or bool(state.get("snapshots"))
        records.extend(file_records)
    if saw_legacy and not any(bool(item.get("derived")) for item in records):
        warnings.append("旧版 token_count 快照缺少 last_token_usage，相关旧数据未纳入 usage 明细")
    return records


def _finalize_usage(records: list[dict[str, Any]], turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_response: dict[str, dict[str, Any]] = {}
    # 无 response_id 的记录当前解析器不产出（真实与派生记录都带 id），此桶
    # 仅防御旧缓存或未来记录形状：保留但不参与按响应去重。
    without_response: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        owner = _as_id(record.get("thread_id"))
        if not owner:
            continue
        # payload.thread_id 是唯一归属来源；源文件所属线程仅用于判断 fork 重复。
        response_id = _as_id(record.get("response_id"))
        if response_id:
            existing = by_response.get(response_id)
            if existing is None or (existing.get("source_thread_id") != owner and record.get("source_thread_id") == owner):
                by_response[response_id] = record
        else:
            marker = str(record.get("id") or "")
            without_response[marker] = record
    ordered = list(by_response.values()) + list(without_response.values())
    result: list[dict[str, Any]] = []
    for record in ordered:
        result.append(
            {
                "id": _as_id(record.get("response_id") or record.get("id")),
                "response_id": _as_id(record.get("response_id")),
                "thread_id": _as_id(record.get("thread_id")),
                "turn_id": _as_id(record.get("turn_id")),
                "root_turn_id": _as_id(record.get("root_turn_id")),
                "timestamp": _as_int(record.get("timestamp")),
                "model": _safe_text(record.get("model"), 160),
                "provider": _safe_text(record.get("provider"), 160),
                "input_tokens": _as_int(record.get("input_tokens"), 0),
                "cached_input_tokens": _as_int(record.get("cached_input_tokens"), 0),
                "cache_write_input": _as_int(record.get("cache_write_input"), 0),
                "output_tokens": _as_int(record.get("output_tokens"), 0),
                "reasoning_output_tokens": _as_int(record.get("reasoning_output_tokens"), 0),
                "total_tokens": _as_int(record.get("total_tokens"), 0),
                "derived": bool(record.get("derived")),
            }
        )
    # envelope timestamp 缺失时回退所属回合 started_at（契约 3.4-1 的回退
    # 分支）；turn 索引只构建一次，复合键避免跨线程回合撞号。
    turn_started: dict[tuple[Any, str], int] = {}
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id = _as_id(turn.get("id"))
        started = _as_int(turn.get("started_at"))
        if turn_id and started is not None:
            turn_started[(turn.get("thread_id"), turn_id)] = started
    for item in result:
        if item["timestamp"] is None:
            item["timestamp"] = turn_started.get((item["thread_id"], str(item["turn_id"] or "")))
    result.sort(key=lambda item: (item.get("timestamp") or 0, str(item.get("id") or "")))
    return result


def _merge_quota(files: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for state in files.values():
        if state.get("missing"):
            continue
        candidates.extend(item for item in state.get("quota", []) if isinstance(item, dict))
    if not candidates:
        return None
    candidates.sort(key=lambda item: _as_int(item.get("captured_at"), 0) or 0)
    return candidates[-1]


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """缓存最近成功平台数据，保持不含任何正文。"""
    allowed = {
        key: snapshot.get(key)
        for key in (
            "platform", "available", "status", "generated_at_ms", "sources", "coverage", "warnings", "threads",
            "turns", "usage", "tools", "edges", "projects", "goals", "environment", "quota", "diagnostics",
        )
    }
    return allowed


def collect_codex(
    home: str | os.PathLike[str] | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    max_bytes: int = 67_108_864,
    max_seconds: float = 8,
) -> dict[str, Any]:
    """读取并聚合 Codex 数据，遵守只读源库和增量缓存边界。"""
    generated_ms = _now_ms()
    snapshot = _empty_snapshot(generated_ms)
    warnings: list[str] = []
    source_home = _resolve_home(home)
    config, configured_sqlite_home = _read_config_metadata(source_home)
    sqlite_home = _resolve_sqlite_home(source_home, configured_sqlite_home)
    dbs = _db_candidates(sqlite_home, source_home)
    cache_path: Path | None = None
    cache: dict[str, Any] = _blank_cache(source_home)
    try:
        _cache_dir, cache_path = _cache_paths(source_home, cache_dir)
        cache = _load_cache(cache_path, source_home, warnings)
    except (OSError, ValueError) as exc:
        warnings.append(f"Codex 增量缓存不可用：{_safe_text(exc, 140) or '未知错误'}")
    previous = cache.get("last_snapshot") if isinstance(cache.get("last_snapshot"), dict) else None
    try:
        state = _read_state(dbs.get("state"), warnings)
        history = _read_history(dbs.get("history"), warnings)
        goals = _read_goals(dbs.get("goals"), warnings)
        threads = state.get("threads", [])
        projects = state.get("projects", [])
        _associate_projects(threads, projects)
        turns = history.get("turns", [])
        tools = history.get("tools", [])
        edges = state.get("edges", [])
        goals_rows = goals.get("goals", [])
        thread_map = {thread.get("id"): thread for thread in threads}
        for turn in turns:
            thread = thread_map.get(turn.get("thread_id"))
            if thread:
                activity = turn.get("completed_at") or turn.get("started_at")
                if activity and (not thread.get("last_activity_at") or activity > thread["last_activity_at"]):
                    thread["last_activity_at"] = activity
                    thread["updated_at"] = max(thread.get("updated_at") or 0, activity)
                    if turn.get("status"):
                        thread["status"] = turn["status"]
        rollout_sources: list[tuple[Path, str | None]] = []
        for thread in threads:
            path = _resolve_rollout_path(thread.get("rollout_path"), source_home)
            if path is not None:
                rollout_sources.append((path, _as_id(thread.get("id"))))
        # rollout_path 只作为内部索引；线程契约不导出源文件绝对路径。
        for thread in threads:
            thread.pop("rollout_path", None)
        processed, total_files, pending_bytes, complete = _process_rollout_files(
            cache.setdefault("files", {}), rollout_sources, max_bytes, max_seconds, warnings
        )
        raw_records = _legacy_usage(cache.get("files", {}), warnings)
        usage = _finalize_usage(raw_records, turns)
        quota = _merge_quota(cache.get("files", {}))
        environment = _read_environment(source_home, sqlite_home, config, dbs)
        log_counts = _read_log_diagnostics(dbs.get("logs"))
        any_source = any(path is not None for path in dbs.values()) or bool(rollout_sources)
        source_ok = state.get("ok") or history.get("ok") or goals.get("ok") or bool(usage)
        # 状态库/历史库分别代表两项独立能力；只存在其中一项时仍可展示，
        # 但必须把平台标为 partial，避免把降级结果误报为完整 ready。
        capability_partial = bool(
            (dbs.get("state") is None and (history.get("ok") or bool(usage)))
            or (dbs.get("history") is None and bool(threads))
            or (dbs.get("goals") is None and bool(goals_rows))
        )
        is_stale = False
        last_success_at: int | None = None
        if not source_ok and previous:
            is_stale = True
            stale = dict(previous)
            threads = stale.get("threads", threads)
            turns = stale.get("turns", turns)
            usage = stale.get("usage", usage)
            tools = stale.get("tools", tools)
            edges = stale.get("edges", edges)
            projects = stale.get("projects", projects)
            goals_rows = stale.get("goals", goals_rows)
            environment = stale.get("environment", environment)
            quota = stale.get("quota", quota)
            # 缓存每轮无条件回存，previous 的 generated_at_ms 在连续 stale 时
            # 已是上一轮刷新时刻；last_success_at 只在成功轮更新，应优先沿用，
            # 缺失时才回退旧口径，否则成功时点会逐轮向前漂移。
            previous_diagnostics = stale.get("diagnostics")
            last_success_at = _as_int(previous_diagnostics.get("last_success_at")) if isinstance(previous_diagnostics, dict) else None
            if last_success_at is None:
                last_success_at = _as_int(stale.get("generated_at_ms"))
            warnings.append("Codex 当前源不可用，保留最近一次成功数据（stale）")
        # n_responses 在 turns 组装处就地补齐：stale 复用的回合同样按当前
        # usage 口径覆盖，避免页面读到缺失键。
        turn_responses: dict[tuple[Any, str], int] = {}
        for item in usage:
            turn_id = item.get("turn_id")
            if turn_id:
                key = (item.get("thread_id"), str(turn_id))
                turn_responses[key] = turn_responses.get(key, 0) + 1
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_id = turn.get("id")
            turn["n_responses"] = turn_responses.get((turn.get("thread_id"), str(turn_id)), 0) if turn_id else 0
        snapshot.update(
            {
                "available": bool(any_source or previous or threads or turns or usage),
                "status": "ready" if source_ok and complete and not warnings and not capability_partial else ("partial" if source_ok or previous else "unavailable"),
                "sources": {
                    "home": str(source_home),
                    "sqlite_home": str(sqlite_home),
                    "state": str(dbs["state"]) if dbs.get("state") else None,
                    "history": str(dbs["history"]) if dbs.get("history") else None,
                    "goals": str(dbs["goals"]) if dbs.get("goals") else None,
                    "logs": str(dbs["logs"]) if dbs.get("logs") else None,
                    "rollouts": len(rollout_sources),
                    "cache": str(cache_path) if cache_path else None,
                },
                "coverage": {
                    "processed_files": processed,
                    "total_files": total_files,
                    "pending_bytes": pending_bytes,
                    "usage_records": len(usage),
                    "complete": complete,
                },
                "warnings": list(dict.fromkeys(warnings)),
                "threads": threads,
                "turns": turns,
                "usage": usage,
                "tools": _dedup(tools, "id"),
                "edges": _dedup(edges, "child_id"),
                "projects": projects,
                "goals": _dedup(goals_rows, "id"),
                "environment": environment,
                "quota": quota,
                "diagnostics": {
                    "cache_schema_version": _CACHE_VERSION,
                    "source_read": {key: bool(value) for key, value in dbs.items()},
                    "log_counts": log_counts,
                    "stale": is_stale,
                    # stale 只保留"最近一次成功"的数据，成功时点也必须一并透出。
                    "last_success_at": last_success_at,
                    "legacy_usage": any(bool(item.get("derived")) for item in usage),
                },
            }
        )
        if cache_path is not None:
            cache["last_snapshot"] = _compact_snapshot(snapshot)
            cache["schema_version"] = _CACHE_VERSION
            cache["source_home"] = str(source_home)
            _write_cache(cache_path, cache, warnings)
            snapshot["warnings"] = list(dict.fromkeys(warnings))
    except Exception as exc:  # 不能让一个损坏能力把整个 widget 变成空白。
        warnings.append(f"Codex 数据聚合失败：{_safe_text(exc, 160) or '未知错误'}")
        if previous:
            snapshot.update(_compact_snapshot(previous))
            snapshot["available"] = True
            snapshot["status"] = "error"
            snapshot["generated_at"] = _now_iso(generated_ms)
            snapshot["generated_at_ms"] = generated_ms
            snapshot["warnings"] = list(dict.fromkeys(warnings + ["已保留最近一次成功数据（stale）"]))
        else:
            snapshot["status"] = "error"
            snapshot["warnings"] = list(dict.fromkeys(warnings))
    return snapshot


def _local_date(ms: Any) -> _dt.date | None:
    value = _timestamp_ms(ms)
    if value is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(value / 1000, tz=_dt.datetime.now().astimezone().tzinfo).date()
    except (OSError, OverflowError, ValueError):
        return None


def _widget_error(snapshot: dict[str, Any]) -> Any:
    candidates: list[tuple[int, str]] = []
    for turn in snapshot.get("turns", []):
        if not isinstance(turn, dict) or not turn.get("error_type"):
            continue
        timestamp = _as_int(turn.get("completed_at") or turn.get("started_at"), 0) or 0
        text = _safe_error_type(turn.get("error_type"))
        if text:
            candidates.append((timestamp, text))
    if candidates:
        timestamp, text = max(candidates)
        return {"type": text, "timestamp": timestamp}
    warnings = snapshot.get("warnings") or []
    if warnings:
        return _safe_text(warnings[-1], 180)
    return None


def codex_widget(snapshot: dict[str, Any], now: Any = None) -> dict[str, Any]:
    """从 collect_codex 快照派生贴纸需要的自然日统计。"""
    if not isinstance(snapshot, dict):
        snapshot = _empty_snapshot()
    current_ms = _timestamp_ms(now) if now is not None else _now_ms()
    current_ms = current_ms or _now_ms()
    current_date = _local_date(current_ms)
    usage = [item for item in snapshot.get("usage", []) if isinstance(item, dict)]
    usable = bool(snapshot.get("available")) and snapshot.get("status") not in {"unavailable"}
    today_records = [item for item in usage if _local_date(item.get("timestamp")) == current_date]
    reliable_today = [item for item in today_records if not bool(item.get("derived"))]

    def total(key: str, source: list[dict[str, Any]]) -> int:
        return sum(_as_int(item.get(key), 0) or 0 for item in source)

    today: dict[str, Any] | None
    week: list[dict[str, Any]]
    if usable:
        today = {
            "requests": len(reliable_today),
            "in": total("input_tokens", today_records),
            "out": total("output_tokens", today_records),
            "cr": total("cached_input_tokens", today_records),
            "reasoning": total("reasoning_output_tokens", today_records),
            "total": total("total_tokens", today_records),
            "tasks": len({item.get("thread_id") for item in today_records if item.get("thread_id")}),
        }
        week = []
        for days_back in range(6, -1, -1):
            day = current_date - _dt.timedelta(days=days_back) if current_date else None
            rows = [item for item in usage if _local_date(item.get("timestamp")) == day]
            reliable = [item for item in rows if not bool(item.get("derived"))]
            week.append(
                {
                    "d": day.strftime("%m-%d") if day else None,
                    "full": day.isoformat() if day else None,
                    "tokens": total("total_tokens", rows),
                    "req": len(reliable),
                }
            )
    else:
        today = {"requests": None, "in": None, "out": None, "cr": None, "reasoning": None, "total": None, "tasks": None}
        week = []
    threads = [item for item in snapshot.get("threads", []) if isinstance(item, dict)]
    recent = sorted(threads, key=lambda item: _as_int(item.get("updated_at"), 0) or 0, reverse=True)[:8]
    recent_tasks = [
        {
            "id": _as_id(item.get("id")),
            "title": _safe_text(item.get("title"), 240),
            "status": _safe_text(item.get("status"), 80),
            "updated_at": _as_int(item.get("updated_at")),
        }
        for item in recent
    ]
    failed_turns = sum(
        1
        for item in snapshot.get("turns", [])
        if isinstance(item, dict)
        and (str(item.get("status", "")).lower() in _FAILURE_STATUSES or item.get("error_type"))
    )
    return {
        "platform": "codex",
        "status": snapshot.get("status", "unavailable"),
        "generated_at": snapshot.get("generated_at"),
        "today": today,
        "week": week,
        "last_error": _widget_error(snapshot),
        "quota": snapshot.get("quota"),
        "coverage": snapshot.get("coverage", {}),
        "recent_tasks": recent_tasks,
        "summary": {
            "threads": len(threads),
            "turns": len(snapshot.get("turns", [])),
            "failed_turns": failed_turns,
        },
    }
