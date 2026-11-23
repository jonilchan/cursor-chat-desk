import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_chat_desk.store import (
    CursorRunning,
    Layout,
    SchemaError,
    UnsafeId,
    delete_chats,
    discover,
    expand_with_children,
    is_cursor_main_process,
    is_safe_id,
    list_chats,
    load_preview,
    load_transcript,
    restore_latest,
)

HEADER_SQL = """
CREATE TABLE composerHeaders (
    composerId TEXT PRIMARY KEY,
    workspaceId TEXT,
    createdAt INTEGER,
    lastUpdatedAt INTEGER,
    isArchived INTEGER,
    isSubagent INTEGER,
    recency INTEGER,
    checkpointAt INTEGER,
    subagentTypeName TEXT,
    value TEXT
);
CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB);
CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB);
"""

SEARCH_SQL = """
CREATE TABLE conversations (
    fts_rowid INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    branches TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    is_archived INTEGER NOT NULL,
    root_fingerprint TEXT,
    cache_fingerprint TEXT,
    UNIQUE(source, scope, id)
);
CREATE VIRTUAL TABLE conversation_fts USING fts5(title, body, branches);
CREATE TABLE conversation_search_candidates (id TEXT PRIMARY KEY, updated_at INTEGER);
"""


def header(name: str, path: str, parent: str | None = None) -> str:
    payload: dict = {
        "name": name,
        "subtitle": "摘要",
        "workspaceIdentifier": {"uri": {"fsPath": path}},
    }
    if parent:
        payload["subagentInfo"] = {"parentComposerId": parent}
    return json.dumps(payload, ensure_ascii=False)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.global_db = root / "global" / "state.vscdb"
        self.search_db = root / "global" / "conversation-search.db"
        self.workspace_root = root / "workspaceStorage"
        self.workspace_db = self.workspace_root / "ws1" / "state.vscdb"
        self.projects = root / "projects"
        self.chats = root / "chats"
        self.backups = root / "backups"
        self.layout = Layout(
            global_db=self.global_db,
            search_db=self.search_db,
            workspace_root=self.workspace_root,
            workspace_dbs=(self.workspace_db,),
            projects_root=self.projects,
            chats_root=self.chats,
            backup_root=self.backups,
        )
        self._init_db(self.global_db)
        self._init_db(self.workspace_db)
        self._init_search(self.search_db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init_db(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3

        connection = sqlite3.connect(path)
        connection.executescript(HEADER_SQL)
        connection.commit()
        connection.close()

    def _init_search(self, path: Path) -> None:
        import sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.executescript(SEARCH_SQL)
        connection.commit()
        connection.close()

    def _add_chat(self, database: Path, composer_id: str, name: str, *, archived: int = 0, subagent: int = 0, parent: str | None = None, updated: int = 10) -> None:
        import sqlite3

        connection = sqlite3.connect(database)
        connection.execute(
            """
            INSERT INTO composerHeaders (
                composerId, workspaceId, createdAt, lastUpdatedAt, isArchived,
                isSubagent, recency, checkpointAt, subagentTypeName, value
            ) VALUES (?, 'ws', 1, ?, ?, ?, 0, 0, '', ?)
            """,
            (composer_id, updated, archived, subagent, header(name, "/tmp/demo-proj", parent)),
        )
        connection.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (f"composerData:{composer_id}", b"{}"),
        )
        connection.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (
                f"bubbleId:{composer_id}:m1",
                json.dumps({"type": 1, "text": f"正文 {name}", "blobEncryptionKey": "super-secret-value"}).encode(),
            ),
        )
        connection.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            (f"glass.agentConversationPanes.{composer_id}", b"{}"),
        )
        connection.commit()
        connection.close()

    def _keys(self, database: Path) -> set[str]:
        import sqlite3

        connection = sqlite3.connect(database)
        keys = {row[0] for row in connection.execute("SELECT key FROM cursorDiskKV")}
        connection.close()
        return keys

    def test_id_rules_and_process_detection(self) -> None:
        self.assertTrue(is_safe_id("da9cfb49-9390-4053-98b4-b317aac6fb08"))
        self.assertTrue(is_safe_id("empty-state-draft"))
        self.assertTrue(is_safe_id("draft-c06cdc39-e737-493c-b911-d5be9a3fdbce"))
        self.assertFalse(is_safe_id("../x"))
        self.assertFalse(is_safe_id("a:b"))
        self.assertFalse(is_safe_id("a/b"))
        self.assertTrue(is_cursor_main_process("/usr/share/cursor/cursor", ["/usr/share/cursor/cursor"]))
        self.assertFalse(
            is_cursor_main_process(
                "/usr/share/cursor/cursor",
                ["/usr/share/cursor/cursor", "--type=zygote"],
            )
        )
        self.assertFalse(
            is_cursor_main_process(
                "/usr/bin/cursor-agent",
                ["/usr/bin/cursor-agent", "worker"],
            )
        )

    def test_list_project_parent_and_skip_unsafe_id(self) -> None:
        self._add_chat(self.global_db, "parent111", "父会话", updated=20)
        self._add_chat(self.global_db, "child2222", "子会话", subagent=1, parent="parent111", updated=30)
        import sqlite3

        connection = sqlite3.connect(self.global_db)
        connection.execute(
            """
            INSERT INTO composerHeaders (
                composerId, workspaceId, createdAt, lastUpdatedAt, isArchived,
                isSubagent, recency, checkpointAt, subagentTypeName, value
            ) VALUES ('../evil', 'ws', 1, 1, 0, 0, 0, 0, '', '{}')
            """
        )
        connection.commit()
        connection.close()
        chats = list_chats(self.layout)
        self.assertEqual([chat.composer_id for chat in chats], ["child2222", "parent111"])
        child = chats[0]
        self.assertEqual(child.project, "demo-proj")
        self.assertEqual(child.parent_id, "parent111")
        self.assertTrue(child.subagent)

    def test_expand_children(self) -> None:
        self._add_chat(self.global_db, "parent111", "父会话")
        self._add_chat(self.global_db, "child2222", "子会话", subagent=1, parent="parent111")
        chats = list_chats(self.layout)
        self.assertEqual(expand_with_children(chats, ["parent111"]), ["parent111", "child2222"])

    def test_delete_keeps_other_chats_and_hash_blobs(self) -> None:
        keep = "draft-aaa-bbb"
        drop = "draft-aaa"
        self._add_chat(self.global_db, drop, "要删")
        self._add_chat(self.global_db, keep, "要留")
        self._add_chat(self.workspace_db, drop, "工作区副本")
        import sqlite3

        connection = sqlite3.connect(self.global_db)
        connection.execute("INSERT INTO cursorDiskKV (key, value) VALUES ('agentKv:blob:abc', 'hash')")
        connection.execute("INSERT INTO cursorDiskKV (key, value) VALUES ('bubbleId:draft-aaa-extra', 'near')")
        connection.execute("INSERT INTO ItemTable (key, value) VALUES ('glass.agentConversationPanes.keep', 'x')")
        connection.commit()
        connection.close()
        folder = self.projects / "demo" / "agent-transcripts" / drop
        folder.mkdir(parents=True)
        (folder / "a.jsonl").write_text("transcript\n", encoding="utf-8")
        other = self.projects / "demo" / "agent-transcripts" / keep
        other.mkdir(parents=True)
        (other / "b.jsonl").write_text("stay\n", encoding="utf-8")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        link = self.projects / "demo" / "agent-transcripts" / "fff111"
        link.symlink_to(outside, target_is_directory=True)
        self._add_chat(self.global_db, "fff111", "链接")

        report = delete_chats(self.layout, [drop, "fff111"], running_check=lambda: False)
        self.assertEqual(set(report.deleted_ids), {drop, "fff111"})
        left = self._keys(self.global_db)
        self.assertIn(f"composerData:{keep}", left)
        self.assertIn(f"bubbleId:{keep}:m1", left)
        self.assertNotIn(f"composerData:{drop}", left)
        self.assertNotIn(f"bubbleId:{drop}:m1", left)
        self.assertIn("agentKv:blob:abc", left)
        self.assertIn("bubbleId:draft-aaa-extra", left)
        self.assertNotIn(f"composerData:{drop}", self._keys(self.workspace_db))
        self.assertFalse(folder.exists())
        self.assertTrue((other / "b.jsonl").is_file())
        self.assertTrue(link.is_symlink())
        self.assertEqual((outside / "secret.txt").read_text(encoding="utf-8"), "secret")
        self.assertIn("跳过符号链接", " ".join(report.warnings))

        restored = restore_latest(self.layout, running_check=lambda: False)
        self.assertGreaterEqual(restored.header_rows, 1)
        self.assertIn(f"composerData:{drop}", self._keys(self.global_db))
        self.assertTrue((folder / "a.jsonl").is_file())
        self.assertEqual((folder / "a.jsonl").read_text(encoding="utf-8"), "transcript\n")

    def test_transcript_keeps_speaker_and_skips_tool_bubbles(self) -> None:
        import sqlite3

        self._add_chat(self.global_db, "parent111", "标题")
        connection = sqlite3.connect(self.global_db)
        connection.execute("DELETE FROM cursorDiskKV WHERE key LIKE 'bubbleId:parent111:%'")
        headers = [
            {"bubbleId": "m-user", "type": 1, "grouping": {"hasText": True}},
            {"bubbleId": "m-tool", "type": 2, "grouping": {"hasText": False}},
            {"bubbleId": "m-cursor", "type": 2, "grouping": {"hasText": True}},
            {"bubbleId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/s:2", "type": 2, "grouping": {"hasText": True}},
        ]
        connection.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            ("composerConvHeaders:parent111:000000", json.dumps(headers)),
        )
        connection.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            ("bubbleId:parent111:m-user", json.dumps({"type": 1, "text": "用户说的", "blobEncryptionKey": "super-secret-value"})),
        )
        connection.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            ("bubbleId:parent111:m-tool", json.dumps({"type": 2, "text": "工具结果不该出现"})),
        )
        connection.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            ("bubbleId:parent111:m-cursor", json.dumps({"type": 2, "text": "Cursor 的回复"})),
        )
        connection.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (
                "bubbleId:parent111:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/s:2",
                json.dumps({"type": 2, "text": "分段回复"}),
            ),
        )
        connection.commit()
        connection.close()
        transcript = load_transcript(self.layout, "parent111")
        self.assertEqual(
            [(item.role, item.text) for item in transcript.messages],
            [("user", "用户说的"), ("cursor", "Cursor 的回复"), ("cursor", "分段回复")],
        )
        self.assertNotIn("super-secret-value", load_preview(self.layout, "parent111"))

    def test_preview_uses_message_text_not_secret_field(self) -> None:
        self._add_chat(self.global_db, "parent111", "标题")
        text = load_preview(self.layout, "parent111")
        self.assertIn("正文 标题", text)
        self.assertNotIn("super-secret-value", text)

    def test_unsafe_id_and_running_cursor_do_not_write(self) -> None:
        self._add_chat(self.global_db, "parent111", "标题")
        before = self._keys(self.global_db)
        with self.assertRaises(UnsafeId):
            delete_chats(self.layout, ["../evil"], running_check=lambda: False)
        self.assertEqual(self._keys(self.global_db), before)

        with self.assertRaises(CursorRunning):
            delete_chats(self.layout, ["parent111"], running_check=lambda: True)
        self.assertEqual(self._keys(self.global_db), before)
        self.assertFalse(self.backups.exists())

    def test_unknown_schema_fails_closed(self) -> None:
        import sqlite3

        empty = Path(self.tmp.name) / "empty.sqlite"
        sqlite3.connect(empty).close()
        layout = Layout(
            global_db=empty,
            search_db=self.search_db,
            workspace_root=self.workspace_root,
            workspace_dbs=(),
            projects_root=self.projects,
            chats_root=self.chats,
            backup_root=self.backups,
        )
        with self.assertRaises(SchemaError):
            delete_chats(layout, ["parent111"], running_check=lambda: False)
        self.assertFalse(self.backups.exists())

    def test_search_row_removed(self) -> None:
        import sqlite3

        self._add_chat(self.global_db, "parent111", "标题")
        connection = sqlite3.connect(self.search_db)
        connection.execute("INSERT INTO conversation_fts (title, body, branches) VALUES ('标题', '索引正文', '')")
        rowid = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """
            INSERT INTO conversations (
                fts_rowid, source, scope, id, title, branches, updated_at,
                is_archived, root_fingerprint, cache_fingerprint
            ) VALUES (?, 'local', '', 'parent111', '标题', '', 1, 0, 'fp', NULL)
            """,
            (rowid,),
        )
        connection.commit()
        connection.close()
        delete_chats(self.layout, ["parent111"], running_check=lambda: False)
        connection = sqlite3.connect(self.search_db)
        left = connection.execute("SELECT count(*) FROM conversations").fetchone()[0]
        connection.close()
        self.assertEqual(left, 0)

    def test_discover_uses_given_home(self) -> None:
        home = Path(self.tmp.name) / "home"
        config = home / "config"
        data = home / "data"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config), "XDG_DATA_HOME": str(data)}):
            layout = discover(home)
        self.assertEqual(layout.global_db, config / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        self.assertEqual(layout.backup_root, data / "cursor-chat-desk" / "backups")
        self.assertEqual(layout.projects_root, home / ".cursor" / "projects")


if __name__ == "__main__":
    unittest.main()
