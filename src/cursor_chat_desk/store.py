"""读取并删除本机 Cursor 会话。

只删除能按会话编号定位的行。编号只允许小写字母、数字和连字符，
查询全部使用参数。agentKv 哈希块不在删除范围内。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

COMPOSER_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BUBBLE_ID_RE = re.compile(
    r"^(?:[a-z0-9]+(?:-[a-z0-9]+)*|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:/s:\d+)?)$"
)
MAX_ID_LENGTH = 80

KEY_PREFIXES = (
    "bubbleId:",
    "checkpointId:",
    "composerData:",
    "composerConvHeaders:",
    "composerDraft:",
    "composerRootWriterDiagnostic:",
    "composerTranscriptHeightTail:",
    "composerTranscriptHeightTailIde:",
    "ofsContent:",
    "codeBlockPartialInlineDiffFates:",
)

HEADER_COLUMNS = (
    "composerId",
    "workspaceId",
    "createdAt",
    "lastUpdatedAt",
    "isArchived",
    "isSubagent",
    "recency",
    "checkpointAt",
    "subagentTypeName",
    "value",
)

PREVIEW_BODY_LIMIT = 12_000
BUBBLE_SCAN_LIMIT = 400
TRANSCRIPT_LIMIT = 50
BUBBLE_TEXT_CHARS = 2_000


class StoreError(Exception):
    """删除或恢复无法安全继续。"""


class SchemaError(StoreError):
    """库结构与当前 Cursor 会话表不一致。"""


class CursorRunning(StoreError):
    """Cursor 主进程仍在运行。"""


class UnsafeId(StoreError):
    """会话编号含有不允许写入查询或路径的字符。"""


@dataclass(frozen=True)
class Layout:
    global_db: Path
    search_db: Path
    workspace_root: Path
    workspace_dbs: tuple[Path, ...]
    projects_root: Path
    chats_root: Path
    backup_root: Path


@dataclass(frozen=True)
class Chat:
    composer_id: str
    title: str
    subtitle: str
    project: str
    project_path: str
    updated_at: int
    created_at: int
    archived: bool
    subagent: bool
    parent_id: str | None


@dataclass(frozen=True)
class TranscriptMessage:
    role: str
    text: str


@dataclass(frozen=True)
class Transcript:
    messages: tuple[TranscriptMessage, ...]
    truncated: bool = False
    note: str = ""


@dataclass(frozen=True)
class DeleteReport:
    backup_dir: Path
    deleted_ids: tuple[str, ...]
    kv_rows: int
    header_rows: int
    search_rows: int
    moved_files: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RestoreReport:
    backup_dir: Path
    header_rows: int
    kv_rows: int
    search_rows: int
    moved_files: int
    warnings: tuple[str, ...]


def discover(home: Path | None = None) -> Layout:
    """按当前用户的配置目录定位 Cursor 库。"""
    home = home or Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    user = config / "Cursor" / "User"
    workspace_root = user / "workspaceStorage"
    databases = tuple(sorted(workspace_root.glob("*/state.vscdb"))) if workspace_root.is_dir() else ()
    return Layout(
        global_db=user / "globalStorage" / "state.vscdb",
        search_db=user / "globalStorage" / "conversation-search.db",
        workspace_root=workspace_root,
        workspace_dbs=databases,
        projects_root=home / ".cursor" / "projects",
        chats_root=home / ".cursor" / "chats",
        backup_root=data / "cursor-chat-desk" / "backups",
    )


def is_bubble_id(bubble_id: str) -> bool:
    return isinstance(bubble_id, str) and len(bubble_id) <= MAX_ID_LENGTH and BUBBLE_ID_RE.fullmatch(bubble_id) is not None


def is_safe_id(composer_id: str) -> bool:
    if not isinstance(composer_id, str) or not composer_id or len(composer_id) > MAX_ID_LENGTH:
        return False
    if any(char in composer_id for char in "*?[]"):
        return False
    return COMPOSER_ID_RE.fullmatch(composer_id) is not None


def require_safe_id(composer_id: str) -> str:
    if not is_safe_id(composer_id):
        raise UnsafeId("会话编号无效，已停止")
    return composer_id


def is_cursor_main_process(exe: str | None, argv: list[str]) -> bool:
    """判断是否为 Cursor 图形界面主进程，排除渲染子进程和 cursor-agent。"""
    exe_name = Path(exe).name if exe else ""
    argv_name = Path(argv[0]).name if argv else ""
    if exe_name != "cursor" and argv_name != "cursor":
        return False
    if any(arg.startswith("--type=") for arg in argv):
        return False
    if any("cursor-agent" in arg for arg in argv):
        return False
    return True


def cursor_is_running() -> bool:
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        exe = None
        try:
            exe = os.readlink(entry / "exe")
        except OSError:
            exe = None
        if is_cursor_main_process(exe, argv):
            return True
    return False


def list_chats(layout: Layout) -> list[Chat]:
    if not layout.global_db.is_file():
        raise FileNotFoundError(layout.global_db)
    with connect(layout.global_db, readonly=True) as con:
        require_known_schema(con)
        rows = con.execute(
            """
            SELECT composerId, workspaceId, createdAt, lastUpdatedAt,
                   isArchived, isSubagent, value
            FROM composerHeaders
            """
        ).fetchall()
    chats: list[Chat] = []
    for row in rows:
        composer_id = row[0] if isinstance(row[0], str) else ""
        if not is_safe_id(composer_id):
            continue
        meta = parse_json_object(row[6])
        project_path, project = project_from_header(meta, row[1])
        parent = parent_id_from_header(meta)
        title = text_field(meta, "name") or composer_id
        chats.append(
            Chat(
                composer_id=composer_id,
                title=title,
                subtitle=text_field(meta, "subtitle"),
                project=project,
                project_path=project_path,
                updated_at=int(row[3] or 0),
                created_at=int(row[2] or 0),
                archived=bool(row[4]),
                subagent=bool(row[5]),
                parent_id=parent,
            )
        )
    chats.sort(key=lambda chat: (chat.updated_at, chat.composer_id), reverse=True)
    return chats


def expand_with_children(chats: list[Chat], selected: list[str]) -> list[str]:
    """把选中会话的子会话一并纳入，只保留安全编号。"""
    valid = {chat.composer_id for chat in chats if is_safe_id(chat.composer_id)}
    children: dict[str, list[str]] = {}
    for chat in chats:
        if chat.parent_id in valid and chat.composer_id in valid:
            children.setdefault(chat.parent_id, []).append(chat.composer_id)
    chosen: list[str] = []
    seen: set[str] = set()
    queue: deque[str] = deque(item for item in selected if item in valid)
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        chosen.append(current)
        for child in children.get(current, []):
            if child not in seen:
                queue.append(child)
    return chosen


def count_owned_keys(layout: Layout, composer_ids: list[str]) -> int:
    ids = [require_safe_id(item) for item in composer_ids]
    if not ids or not layout.global_db.is_file():
        return 0
    total = 0
    with connect(layout.global_db, readonly=True) as con:
        require_known_schema(con)
        for composer_id in ids:
            total += _count_keys(con, composer_id)
    return total


def load_transcript(layout: Layout, composer_id: str) -> Transcript:
    """按时间读出有正文的消息，并标出用户或 Cursor。"""
    composer_id = require_safe_id(composer_id)
    if not layout.global_db.is_file():
        return Transcript((), note="这条会话没有可显示的正文。")
    try:
        with connect(layout.global_db, readonly=True) as connection:
            if not _table_exists(connection, "cursorDiskKV"):
                return Transcript((), note="这条会话没有可显示的正文。")
            ordered = _ordered_text_bubbles(connection, composer_id)
            truncated = False
            if ordered:
                truncated = len(ordered) > TRANSCRIPT_LIMIT
                ordered = ordered[-TRANSCRIPT_LIMIT:]
                messages = _messages_for_ids(connection, composer_id, ordered)
            else:
                messages, truncated = _messages_from_bubble_scan(connection, composer_id)
    except sqlite3.Error:
        messages, truncated = [], False
    if messages:
        return Transcript(tuple(messages), truncated=truncated)
    return Transcript((), note="这条会话没有可显示的正文。")


def load_preview(layout: Layout, composer_id: str) -> str:
    """把正文拼成纯文本，供不需要区分角色的调用使用。"""
    transcript = load_transcript(layout, composer_id)
    if not transcript.messages:
        return transcript.note or "这条会话没有可显示的正文。"
    body = "\n\n".join(message.text for message in transcript.messages)
    if len(body) > PREVIEW_BODY_LIMIT:
        return body[:PREVIEW_BODY_LIMIT] + "\n\n（预览已截断）"
    return body


def delete_chats(
    layout: Layout,
    composer_ids: list[str],
    *,
    running_check=cursor_is_running,
    now: datetime | None = None,
) -> DeleteReport:
    """备份后删除会话行、搜索索引，并把转录目录移入备份。"""
    if running_check():
        raise CursorRunning("Cursor 正在运行，已停止删除")
    ids = list(dict.fromkeys(require_safe_id(item) for item in composer_ids))
    if not ids:
        raise StoreError("没有可删除的会话")
    if not layout.global_db.is_file():
        raise FileNotFoundError(layout.global_db)
    with connect(layout.global_db, readonly=True) as con:
        require_known_schema(con)

    backup_dir = _prepare_backup_dir(layout.backup_root, now or datetime.now())
    warnings: list[str] = []
    deleted = False
    try:
        kv_copied, header_copied, search_copied, file_plan = _write_backup(layout, ids, backup_dir, warnings)
        _write_json(
            backup_dir / "manifest.json",
            {
                "createdAt": datetime.now().isoformat(timespec="seconds"),
                "composerIds": ids,
                "kvRows": kv_copied,
                "headerRows": header_copied,
                "searchRows": search_copied,
                "movedFiles": 0,
            },
        )
        if running_check():
            raise CursorRunning("Cursor 在备份期间启动了，已停止删除")
        kv_rows, header_rows = _delete_global(layout.global_db, ids)
        deleted = True
        for database in layout.workspace_dbs:
            if not _workspace_db_ok(database, layout.workspace_root):
                warnings.append(f"跳过工作区库 {database.parent.name}")
                continue
            try:
                _delete_state_file(database, ids, require_schema=False)
            except (sqlite3.Error, SchemaError, OSError) as exc:
                warnings.append(f"工作区库未清理：{exc.__class__.__name__}")
        search_rows = 0
        if layout.search_db.is_file():
            try:
                search_rows = _delete_search(layout.search_db, ids)
            except sqlite3.Error:
                warnings.append("搜索索引未清理")
        moved, move_warnings = _move_transcripts(file_plan, backup_dir)
        warnings.extend(move_warnings)
        _write_json(
            backup_dir / "manifest.json",
            {
                "createdAt": datetime.now().isoformat(timespec="seconds"),
                "composerIds": ids,
                "kvRows": kv_copied,
                "headerRows": header_copied,
                "searchRows": search_copied,
                "movedFiles": moved,
            },
        )
    except Exception:
        if not deleted and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        raise
    return DeleteReport(
        backup_dir=backup_dir,
        deleted_ids=tuple(ids),
        kv_rows=kv_rows,
        header_rows=header_rows,
        search_rows=search_rows,
        moved_files=moved,
        warnings=tuple(warnings),
    )


def latest_backup(layout: Layout) -> Path | None:
    root = layout.backup_root
    if not root.is_dir():
        return None
    found = [
        path
        for path in root.iterdir()
        if path.is_dir() and (path / "manifest.json").is_file() and (path / "rows.sqlite").is_file()
    ]
    if not found:
        return None
    return max(found, key=lambda path: path.name)


def restore_latest(
    layout: Layout,
    *,
    running_check=cursor_is_running,
) -> RestoreReport:
    if running_check():
        raise CursorRunning("Cursor 正在运行，已停止恢复")
    backup_dir = latest_backup(layout)
    if backup_dir is None:
        raise StoreError("没有可恢复的备份")
    if not _is_within(backup_dir, layout.backup_root):
        raise StoreError("备份目录不在允许的位置")
    with connect(layout.global_db, readonly=True) as con:
        require_known_schema(con)
    warnings: list[str] = []
    with connect(backup_dir / "rows.sqlite", readonly=True) as backup:
        header_rows = _restore_headers(layout, backup, warnings)
        kv_rows = _restore_kv(layout.global_db, backup)
        search_rows = _restore_search(layout.search_db, backup, warnings)
    moved = _restore_files(layout, backup_dir, warnings)
    return RestoreReport(
        backup_dir=backup_dir,
        header_rows=header_rows,
        kv_rows=kv_rows,
        search_rows=search_rows,
        moved_files=moved,
        warnings=tuple(warnings),
    )


def db_size_label(path: Path) -> str:
    if not path.is_file():
        return "库文件不存在"
    size = path.stat().st_size
    gib = size / (1024**3)
    if gib >= 1:
        return f"{gib:.1f} GB"
    return f"{size / (1024**2):.0f} MB"


def format_time(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d %H:%M")


@contextmanager
def connect(path: Path, *, readonly: bool) -> Iterator[sqlite3.Connection]:
    if readonly:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA busy_timeout=8000")
        yield connection
    finally:
        connection.close()


def require_known_schema(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "composerHeaders") or not _table_exists(connection, "cursorDiskKV"):
        raise SchemaError("缺少 composerHeaders 或 cursorDiskKV，已停止")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(composerHeaders)")}
    if any(name not in columns for name in HEADER_COLUMNS):
        raise SchemaError("composerHeaders 字段无法识别，已停止")


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _owned_clause() -> str:
    return "key = ? OR (key >= ? AND key < ?)"


def _owned_params(prefix: str, composer_id: str) -> tuple[str, str, str]:
    exact = prefix + composer_id
    return exact, exact + ":", exact + ";"


def _count_keys(connection: sqlite3.Connection, composer_id: str) -> int:
    total = 0
    for prefix in KEY_PREFIXES:
        row = connection.execute(
            f"SELECT count(*) FROM cursorDiskKV WHERE {_owned_clause()}",
            _owned_params(prefix, composer_id),
        ).fetchone()
        total += int(row[0])
    return total


def _iter_owned(connection: sqlite3.Connection, composer_id: str) -> Iterator[tuple[str, object]]:
    for prefix in KEY_PREFIXES:
        yield from connection.execute(
            f"SELECT key, value FROM cursorDiskKV WHERE {_owned_clause()}",
            _owned_params(prefix, composer_id),
        )


def _delete_owned(connection: sqlite3.Connection, composer_id: str) -> int:
    deleted = 0
    for prefix in KEY_PREFIXES:
        cursor = connection.execute(
            f"DELETE FROM cursorDiskKV WHERE {_owned_clause()}",
            _owned_params(prefix, composer_id),
        )
        deleted += cursor.rowcount if cursor.rowcount != -1 else 0
    if _table_exists(connection, "ItemTable"):
        cursor = connection.execute(
            "DELETE FROM ItemTable WHERE key = ?",
            (f"glass.agentConversationPanes.{composer_id}",),
        )
        deleted += cursor.rowcount if cursor.rowcount != -1 else 0
    return deleted


def _prepare_backup_dir(root: Path, now: datetime) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    target = root / stamp
    suffix = 0
    while target.exists():
        suffix += 1
        target = root / f"{stamp}-{suffix}"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    return target


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _write_backup(
    layout: Layout,
    composer_ids: list[str],
    backup_dir: Path,
    warnings: list[str],
) -> tuple[int, int, int, list[tuple[Path, str]]]:
    database = backup_dir / "rows.sqlite"
    kv_rows = 0
    header_rows = 0
    search_rows = 0
    file_plan: list[tuple[Path, str]] = []
    with connect(database, readonly=False) as backup:
        backup.execute(
            """
            CREATE TABLE composerHeaders (
                composerId TEXT,
                workspaceId TEXT,
                createdAt INTEGER,
                lastUpdatedAt INTEGER,
                isArchived INTEGER,
                isSubagent INTEGER,
                recency INTEGER,
                checkpointAt INTEGER,
                subagentTypeName TEXT,
                value TEXT,
                sourceDb TEXT,
                PRIMARY KEY (composerId, sourceDb)
            )
            """
        )
        backup.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
        backup.execute(
            """
            CREATE TABLE searchConversations (
                id TEXT PRIMARY KEY,
                source TEXT,
                scope TEXT,
                title TEXT,
                branches TEXT,
                updated_at INTEGER,
                is_archived INTEGER,
                root_fingerprint TEXT,
                cache_fingerprint TEXT,
                body TEXT
            )
            """
        )
        with connect(layout.global_db, readonly=True) as source:
            copied_headers, copied_kv = _copy_state(source, backup, composer_ids, str(layout.global_db))
            header_rows += copied_headers
            kv_rows += copied_kv
        for workspace_db in layout.workspace_dbs:
            if not _workspace_db_ok(workspace_db, layout.workspace_root):
                continue
            try:
                with connect(workspace_db, readonly=True) as source:
                    if not _table_exists(source, "composerHeaders"):
                        continue
                    copied_headers, copied_kv = _copy_state(source, backup, composer_ids, str(workspace_db))
                    header_rows += copied_headers
                    kv_rows += copied_kv
            except (sqlite3.Error, OSError):
                warnings.append("某个工作区库未能备份")
        if layout.search_db.is_file():
            try:
                search_rows = _copy_search(layout.search_db, backup, composer_ids)
            except sqlite3.Error:
                warnings.append("搜索索引未能备份")
        backup.commit()
    os.chmod(database, 0o600)
    index = 0
    for composer_id in composer_ids:
        paths, notes = find_transcript_dirs(layout, composer_id)
        warnings.extend(notes)
        for path in paths:
            file_plan.append((path, f"files/{index}"))
            index += 1
    _write_json(
        backup_dir / "file-plan.json",
        [{"source": str(path), "backup": rel, "id": path.name} for path, rel in file_plan],
    )
    return kv_rows, header_rows, search_rows, file_plan


def _copy_state(
    source: sqlite3.Connection,
    backup: sqlite3.Connection,
    composer_ids: list[str],
    source_db: str,
) -> tuple[int, int]:
    headers = 0
    kv_rows = 0
    for composer_id in composer_ids:
        rows = source.execute(
            f"SELECT {', '.join(HEADER_COLUMNS)} FROM composerHeaders WHERE composerId = ?",
            (composer_id,),
        ).fetchall()
        for row in rows:
            backup.execute(
                f"""
                INSERT OR REPLACE INTO composerHeaders
                ({', '.join(HEADER_COLUMNS)}, sourceDb)
                VALUES ({', '.join('?' for _ in HEADER_COLUMNS)}, ?)
                """,
                (*row, source_db),
            )
            headers += 1
        for key, value in _iter_owned(source, composer_id):
            backup.execute(
                "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (key, value),
            )
            kv_rows += 1
    return headers, kv_rows


def _copy_search(search_db: Path, backup: sqlite3.Connection, composer_ids: list[str]) -> int:
    copied = 0
    with connect(search_db, readonly=True) as source:
        if not _table_exists(source, "conversations"):
            return 0
        for composer_id in composer_ids:
            try:
                rows = source.execute(
                    """
                    SELECT c.id, c.source, c.scope, c.title, c.branches, c.updated_at,
                           c.is_archived, c.root_fingerprint, c.cache_fingerprint, f.c1
                    FROM conversations c
                    LEFT JOIN conversation_fts_content f ON f.id = c.fts_rowid
                    WHERE c.id = ? AND c.source = 'local'
                    """,
                    (composer_id,),
                ).fetchall()
            except sqlite3.Error:
                rows = source.execute(
                    """
                    SELECT id, source, scope, title, branches, updated_at,
                           is_archived, root_fingerprint, cache_fingerprint, NULL
                    FROM conversations
                    WHERE id = ? AND source = 'local'
                    """,
                    (composer_id,),
                ).fetchall()
            for row in rows:
                backup.execute(
                    """
                    INSERT OR REPLACE INTO searchConversations (
                        id, source, scope, title, branches, updated_at,
                        is_archived, root_fingerprint, cache_fingerprint, body
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                copied += 1
    return copied


def _delete_global(database: Path, composer_ids: list[str]) -> tuple[int, int]:
    with connect(database, readonly=False) as connection:
        require_known_schema(connection)
        try:
            connection.execute("BEGIN")
            kv_rows, header_rows = _delete_loaded(connection, composer_ids)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return kv_rows, header_rows


def _delete_state_file(database: Path, composer_ids: list[str], *, require_schema: bool) -> None:
    with connect(database, readonly=False) as connection:
        if require_schema:
            require_known_schema(connection)
        elif not _table_exists(connection, "composerHeaders"):
            return
        try:
            connection.execute("BEGIN")
            _delete_loaded(connection, composer_ids)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _delete_loaded(connection: sqlite3.Connection, composer_ids: list[str]) -> tuple[int, int]:
    kv_rows = 0
    header_rows = 0
    for composer_id in composer_ids:
        if _table_exists(connection, "cursorDiskKV"):
            kv_rows += _delete_owned(connection, composer_id)
        if _table_exists(connection, "composerHeaders"):
            cursor = connection.execute(
                "DELETE FROM composerHeaders WHERE composerId = ?",
                (composer_id,),
            )
            header_rows += cursor.rowcount if cursor.rowcount != -1 else 0
    return kv_rows, header_rows


def _delete_search(database: Path, composer_ids: list[str]) -> int:
    deleted = 0
    with connect(database, readonly=False) as connection:
        if not _table_exists(connection, "conversations"):
            return 0
        try:
            connection.execute("BEGIN")
            for composer_id in composer_ids:
                rows = connection.execute(
                    "SELECT fts_rowid FROM conversations WHERE id = ? AND source = 'local'",
                    (composer_id,),
                ).fetchall()
                for (rowid,) in rows:
                    if _table_exists(connection, "conversation_fts"):
                        connection.execute("DELETE FROM conversation_fts WHERE rowid = ?", (rowid,))
                cursor = connection.execute(
                    "DELETE FROM conversations WHERE id = ? AND source = 'local'",
                    (composer_id,),
                )
                deleted += cursor.rowcount if cursor.rowcount != -1 else 0
                if _table_exists(connection, "conversation_search_candidates"):
                    connection.execute(
                        "DELETE FROM conversation_search_candidates WHERE id = ?",
                        (composer_id,),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return deleted


def find_transcript_dirs(layout: Layout, composer_id: str) -> tuple[list[Path], list[str]]:
    composer_id = require_safe_id(composer_id)
    found: list[Path] = []
    warnings: list[str] = []
    specs = (
        (layout.projects_root, ("agent-transcripts",)),
        (layout.chats_root, ()),
    )
    for root, middle in specs:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            candidate = child.joinpath(*middle, composer_id)
            if candidate.is_symlink():
                warnings.append("跳过符号链接转录目录")
                continue
            if not candidate.is_dir() or candidate.name != composer_id:
                continue
            if not _is_within(candidate, root):
                warnings.append("跳过超出允许目录的转录")
                continue
            found.append(candidate)
    return found, warnings


def _move_transcripts(file_plan: list[tuple[Path, str]], backup_dir: Path) -> tuple[int, list[str]]:
    moved = 0
    warnings: list[str] = []
    for source, relative in file_plan:
        destination = backup_dir / relative
        if not _is_within(destination, backup_dir):
            warnings.append("跳过非法备份路径")
            continue
        if not source.is_dir() or source.is_symlink():
            warnings.append("转录目录已不在原处")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        moved += 1
    return moved, warnings


def _restore_headers(layout: Layout, backup: sqlite3.Connection, warnings: list[str]) -> int:
    rows = backup.execute(
        f"SELECT {', '.join(HEADER_COLUMNS)}, sourceDb FROM composerHeaders"
    ).fetchall()
    grouped: dict[str, list[tuple]] = {}
    for row in rows:
        grouped.setdefault(row[-1], []).append(row[:-1])
    restored = 0
    for source_db, items in grouped.items():
        target = Path(source_db)
        if target == layout.global_db:
            allowed = True
        else:
            allowed = _workspace_db_ok(target, layout.workspace_root)
        if not allowed or not target.is_file():
            warnings.append("有一份会话头无法写回原库")
            continue
        with connect(target, readonly=False) as connection:
            if not _table_exists(connection, "composerHeaders"):
                warnings.append("目标库没有 composerHeaders")
                continue
            connection.execute("BEGIN")
            try:
                for item in items:
                    connection.execute(
                        f"""
                        INSERT OR REPLACE INTO composerHeaders ({', '.join(HEADER_COLUMNS)})
                        VALUES ({', '.join('?' for _ in HEADER_COLUMNS)})
                        """,
                        item,
                    )
                    restored += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    return restored


def _restore_kv(database: Path, backup: sqlite3.Connection) -> int:
    rows = backup.execute("SELECT key, value FROM cursorDiskKV").fetchall()
    if not rows:
        return 0
    with connect(database, readonly=False) as connection:
        require_known_schema(connection)
        connection.execute("BEGIN")
        try:
            connection.executemany(
                "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
                rows,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return len(rows)


def _restore_search(database: Path, backup: sqlite3.Connection, warnings: list[str]) -> int:
    rows = backup.execute(
        """
        SELECT id, source, scope, title, branches, updated_at,
               is_archived, root_fingerprint, cache_fingerprint, body
        FROM searchConversations
        """
    ).fetchall()
    if not rows:
        return 0
    if not database.is_file():
        warnings.append("搜索库不存在，跳过搜索恢复")
        return 0
    restored = 0
    with connect(database, readonly=False) as connection:
        if not _table_exists(connection, "conversations") or not _table_exists(connection, "conversation_fts"):
            warnings.append("搜索库结构无法识别")
            return 0
        connection.execute("BEGIN")
        try:
            for row in rows:
                body = row[9] if isinstance(row[9], str) else ""
                connection.execute(
                    "INSERT INTO conversation_fts (title, body, branches) VALUES (?, ?, ?)",
                    (row[3] or "", body or "", row[4] or ""),
                )
                fts_rowid = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                connection.execute(
                    """
                    INSERT OR REPLACE INTO conversations (
                        fts_rowid, source, scope, id, title, branches, updated_at,
                        is_archived, root_fingerprint, cache_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (fts_rowid, row[1], row[2], row[0], row[3], row[4], row[5], row[6], row[7], row[8]),
                )
                restored += 1
            connection.commit()
        except Exception:
            connection.rollback()
            warnings.append("搜索索引未能恢复")
            return 0
    return restored


def _restore_files(layout: Layout, backup_dir: Path, warnings: list[str]) -> int:
    plan_path = backup_dir / "file-plan.json"
    if not plan_path.is_file():
        return 0
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        warnings.append("备份里的文件清单无法读取")
        return 0
    if not isinstance(plan, list):
        warnings.append("备份里的文件清单无法读取")
        return 0
    moved = 0
    for item in plan:
        if not isinstance(item, dict):
            continue
        relative = item.get("backup")
        source_text = item.get("source")
        composer_id = item.get("id")
        if not isinstance(relative, str) or not isinstance(source_text, str) or not isinstance(composer_id, str):
            warnings.append("跳过一条无效的文件记录")
            continue
        if not is_safe_id(composer_id):
            warnings.append("跳过编号无效的转录")
            continue
        stored = (backup_dir / relative).resolve()
        if not _is_within(stored, backup_dir) or not stored.is_dir() or stored.is_symlink():
            warnings.append("跳过不在备份目录内的转录")
            continue
        destination = Path(source_text)
        if not _transcript_destination_ok(destination, composer_id, layout):
            warnings.append("跳过不允许写回的转录路径")
            continue
        if destination.exists():
            warnings.append("原转录目录已存在，未覆盖")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stored), str(destination))
        moved += 1
    return moved


def _transcript_destination_ok(destination: Path, composer_id: str, layout: Layout) -> bool:
    if destination.name != composer_id or destination.is_symlink():
        return False
    parent = destination.parent
    if not parent.exists() or parent.is_symlink():
        return False
    resolved = parent.resolve() / destination.name
    return _matches_transcript_layout(resolved, composer_id, layout.projects_root, ("agent-transcripts",)) or _matches_transcript_layout(
        resolved, composer_id, layout.chats_root, ()
    )


def _matches_transcript_layout(resolved: Path, composer_id: str, root: Path, middle: tuple[str, ...]) -> bool:
    if not root.exists():
        return False
    try:
        relative = resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    expected = ("*", *middle, composer_id)
    if len(relative.parts) != len(expected):
        return False
    if relative.parts[-1] != composer_id:
        return False
    for part, expect in zip(relative.parts[1:-1], middle):
        if part != expect:
            return False
    return True


def _workspace_db_ok(database: Path, workspace_root: Path) -> bool:
    if database.name != "state.vscdb":
        return False
    try:
        relative = database.resolve().relative_to(workspace_root.resolve())
    except (OSError, ValueError):
        return False
    return len(relative.parts) == 2


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _search_body(database: Path, composer_id: str) -> str:
    with connect(database, readonly=True) as connection:
        if not _table_exists(connection, "conversations"):
            return ""
        try:
            row = connection.execute(
                """
                SELECT f.c1
                FROM conversations c
                JOIN conversation_fts_content f ON f.id = c.fts_rowid
                WHERE c.id = ? AND c.source = 'local'
                """,
                (composer_id,),
            ).fetchone()
        except sqlite3.Error:
            return ""
    if not row:
        return ""
    return _as_text(row[0]) or ""


def _role(type_value: object) -> str | None:
    if type_value == 1:
        return "user"
    if type_value == 2:
        return "cursor"
    return None


def _ordered_text_bubbles(connection: sqlite3.Connection, composer_id: str) -> list[tuple[str, str]]:
    start = f"composerConvHeaders:{composer_id}:"
    end = f"composerConvHeaders:{composer_id};"
    ordered: list[tuple[str, str]] = []
    rows = connection.execute(
        "SELECT value FROM cursorDiskKV WHERE key >= ? AND key < ? ORDER BY key",
        (start, end),
    )
    for (value,) in rows:
        try:
            payload = json.loads(_as_text(value) or "null")
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            grouping = item.get("grouping")
            if isinstance(grouping, dict) and not grouping.get("hasText"):
                continue
            role = _role(item.get("type"))
            bubble_id = item.get("bubbleId")
            if role is None or not is_bubble_id(bubble_id):
                continue
            ordered.append((role, bubble_id))
    return ordered


def _messages_for_ids(
    connection: sqlite3.Connection,
    composer_id: str,
    ordered: list[tuple[str, str]],
) -> list[TranscriptMessage]:
    messages: list[TranscriptMessage] = []
    for role, bubble_id in ordered:
        row = connection.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"bubbleId:{composer_id}:{bubble_id}",),
        ).fetchone()
        if row is None:
            continue
        text = _message_text(parse_json_object(row[0]))
        if text:
            messages.append(TranscriptMessage(role, text))
    return messages


def _messages_from_bubble_scan(
    connection: sqlite3.Connection,
    composer_id: str,
) -> tuple[list[TranscriptMessage], bool]:
    start = f"bubbleId:{composer_id}:"
    end = f"bubbleId:{composer_id};"
    found: list[tuple[str, TranscriptMessage]] = []
    rows = connection.execute(
        """
        SELECT value FROM cursorDiskKV
        WHERE key >= ? AND key < ?
        LIMIT ?
        """,
        (start, end, BUBBLE_SCAN_LIMIT),
    )
    for (value,) in rows:
        payload = parse_json_object(value)
        role = _role(payload.get("type"))
        text = _message_text(payload)
        if role is None or not text:
            continue
        created = payload.get("createdAt")
        stamp = created if isinstance(created, str) else ""
        found.append((stamp, TranscriptMessage(role, text)))
    found.sort(key=lambda item: item[0])
    truncated = len(found) > TRANSCRIPT_LIMIT
    return [message for _stamp, message in found[-TRANSCRIPT_LIMIT:]], truncated


def _message_text(payload: dict) -> str:
    text = payload.get("text")
    if not isinstance(text, str):
        return ""
    snippet = text.strip()
    if not snippet:
        return ""
    if len(snippet) > BUBBLE_TEXT_CHARS:
        return snippet[:BUBBLE_TEXT_CHARS] + "…"
    return snippet


def parse_json_object(value: object) -> dict:
    text = _as_text(value)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _as_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def text_field(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def project_from_header(payload: dict, workspace_id: object) -> tuple[str, str]:
    identifier = payload.get("workspaceIdentifier")
    path = ""
    if isinstance(identifier, dict):
        uri = identifier.get("uri")
        if isinstance(uri, dict):
            raw = uri.get("fsPath") or uri.get("path") or ""
            if isinstance(raw, str):
                path = raw
    if path:
        name = Path(path).name or path
        return path, name
    if isinstance(workspace_id, str) and workspace_id:
        return "", workspace_id[:8]
    return "", "（无工作区）"


def parent_id_from_header(payload: dict) -> str | None:
    info = payload.get("subagentInfo")
    if not isinstance(info, dict):
        return None
    parent = info.get("parentComposerId")
    if isinstance(parent, str) and is_safe_id(parent):
        return parent
    return None
