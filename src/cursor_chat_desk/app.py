"""本机窗口：列出会话，确认后删除。"""

from __future__ import annotations

import re
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from cursor_chat_desk.store import (
    Chat,
    Layout,
    count_owned_keys,
    cursor_is_running,
    db_size_label,
    delete_chats,
    discover,
    expand_with_children,
    format_time,
    latest_backup,
    list_chats,
    Transcript,
    load_transcript,
    restore_latest,
)

STYLE = """
.sidebar-pane {
  background: @sidebar_bg_color;
}
.chat-list row {
  border-radius: 12px;
  margin: 1px 8px;
}
.chat-badge {
  border-radius: 999px;
  padding: 1px 8px;
  font-weight: 600;
  background: alpha(@window_fg_color, 0.08);
  color: alpha(@window_fg_color, 0.72);
}
.chat-badge.current {
  background: alpha(@accent_bg_color, 0.18);
  color: @accent_color;
}
.muted-title {
  color: alpha(@window_fg_color, 0.58);
}
.msg {
  border-radius: 16px;
  padding: 8px 12px;
}
.msg-user {
  background: alpha(@accent_bg_color, 0.16);
}
.msg-cursor {
  background: alpha(@window_fg_color, 0.06);
}
.msg-user .who {
  color: @accent_color;
  font-weight: 700;
}
.msg-cursor .who {
  font-weight: 700;
}
.pane-footer {
  padding: 4px 12px;
  min-height: 0;
}
.pane-footer button {
  min-height: 0;
  padding: 2px 10px;
}
"""


class ChatObject(GObject.Object):
    composer_id = GObject.Property(type=str, default="")
    heading = GObject.Property(type=str, default="")
    detail = GObject.Property(type=str, default="")
    badge = GObject.Property(type=str, default="")
    muted = GObject.Property(type=bool, default=False)


class RowBox(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.set_hexpand(True)
        self.set_margin_top(9)
        self.set_margin_bottom(9)
        self.set_margin_start(12)
        self.set_margin_end(12)
        top = Gtk.Box(spacing=8)
        self.heading = Gtk.Label(xalign=0, hexpand=True)
        self.heading.set_ellipsize(Pango.EllipsizeMode.END)
        self.heading.add_css_class("heading")
        self.badge = Gtk.Label()
        self.badge.add_css_class("caption")
        self.badge.add_css_class("chat-badge")
        self.badge.set_valign(Gtk.Align.CENTER)
        top.append(self.heading)
        top.append(self.badge)
        self.detail = Gtk.Label(xalign=0)
        self.detail.set_ellipsize(Pango.EllipsizeMode.END)
        self.detail.add_css_class("caption")
        self.detail.add_css_class("dim-label")
        self.append(top)
        self.append(self.detail)


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def display_title(chat: Chat) -> str:
    title = (chat.title or "").strip()
    if not title or title == chat.composer_id or _UUID.fullmatch(title):
        return "未命名"
    return title


def _button(icon_name: str, label: str) -> Gtk.Button:
    button = Gtk.Button()
    button.set_child(Adw.ButtonContent(icon_name=icon_name, label=label))
    return button


def _clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


class DeskWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="Cursor 会话")
        self.set_default_size(1120, 760)
        self.layout: Layout | None = None
        self.chats: list[Chat] = []
        self.by_id: dict[str, Chat] = {}
        self.query = ""
        self.mode = "all"
        self.show_subagents = False
        self._busy = False
        self._running = False
        self._selection_count = 0
        self._preview_gen = 0
        self._load_error = ""
        self._pending_ids: list[str] = []
        self._install_style()

        self.window_title = Adw.WindowTitle.new("Cursor 会话", "正在读取")
        header = Adw.HeaderBar()
        header.set_title_widget(self.window_title)
        self.refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_btn.set_tooltip_text("重新读取")
        self.refresh_btn.connect("clicked", lambda _button: self.refresh())
        header.pack_start(self.refresh_btn)

        self.banner = Adw.Banner()
        self.banner.set_revealed(False)

        self.mode_group = Adw.ToggleGroup()
        self.mode_group.set_homogeneous(True)
        self.mode_group.set_hexpand(True)
        self.mode_group.set_can_shrink(True)
        for name, label in (("all", "全部"), ("active", "使用中"), ("archived", "已归档")):
            self.mode_group.add(Adw.Toggle(name=name, label=label))
        self.mode_group.set_active_name("all")
        self.mode_group.connect("notify::active-name", self._on_mode)
        filter_wrap = Gtk.Box()
        filter_wrap.set_margin_top(12)
        filter_wrap.set_margin_bottom(8)
        filter_wrap.set_margin_start(12)
        filter_wrap.set_margin_end(12)
        filter_wrap.append(self.mode_group)

        self.search = Gtk.SearchEntry(placeholder_text="搜索标题、项目或编号", hexpand=True)
        self.search.set_margin_start(12)
        self.search.set_margin_end(12)
        self.search.set_margin_bottom(8)
        self.search.connect("changed", self._on_search)

        self.store = Gio.ListStore.new(ChatObject)
        self.selection = Gtk.MultiSelection.new(self.store)
        self.selection.connect("selection-changed", self._on_selection)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_row)
        factory.connect("bind", self._bind_row)
        self.list_view = Gtk.ListView(model=self.selection, factory=factory, vexpand=True)
        self.list_view.add_css_class("navigation-sidebar")
        self.list_view.add_css_class("chat-list")
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.list_view.add_controller(keys)
        list_scroll = Gtk.ScrolledWindow(child=self.list_view, vexpand=True)
        list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.sub_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.sub_switch.connect("notify::active", self._on_subagents)
        self.count_label = Gtk.Label(xalign=0, hexpand=True)
        self.count_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.count_label.add_css_class("caption")
        self.count_label.add_css_class("dim-label")
        switch_label = Gtk.Label(label="子会话")
        switch_label.set_tooltip_text("显示子会话")
        switch_label.add_css_class("caption")
        list_footer = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        list_footer.add_css_class("pane-footer")
        list_footer.append(self.count_label)
        list_footer.append(switch_label)
        list_footer.append(self.sub_switch)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=380)
        sidebar.add_css_class("sidebar-pane")
        sidebar.append(filter_wrap)
        sidebar.append(self.search)
        sidebar.append(list_scroll)
        sidebar.append(Gtk.Separator())
        sidebar.append(list_footer)

        self.preview_title = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.preview_title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.preview_title.add_css_class("title-1")
        self.preview_badges = Gtk.Box(spacing=6)
        self.preview_meta = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.preview_meta.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.preview_meta.add_css_class("dim-label")
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading.set_margin_top(28)
        heading.set_margin_bottom(8)
        heading.set_margin_start(32)
        heading.set_margin_end(32)
        heading.append(self.preview_title)
        heading.append(self.preview_badges)
        heading.append(self.preview_meta)

        self.transcript = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.transcript.set_hexpand(True)
        self.transcript.set_margin_top(16)
        self.transcript.set_margin_bottom(20)
        self.transcript.set_margin_start(20)
        self.transcript.set_margin_end(20)
        preview_scroll = Gtk.ScrolledWindow(child=self.transcript, hexpand=True, vexpand=True)
        preview_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.transcript_scroll = preview_scroll
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(heading)
        content.append(Gtk.Separator())
        content.append(preview_scroll)

        self.empty_page = Adw.StatusPage(
            icon_name="view-list-symbolic",
            title="选择一条会话",
            description="点左侧的一条对话，这里会显示正文。\n按住 Ctrl 可以多选，再一起删除。",
            vexpand=True,
        )
        self.preview_stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.preview_stack.add_named(self.empty_page, "empty")
        self.preview_stack.add_named(content, "content")
        self.preview_stack.set_visible_child_name("empty")

        self.status = Gtk.Label(xalign=0, hexpand=True)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.add_css_class("caption")
        self.status.add_css_class("dim-label")
        self.restore_btn = _button("document-revert-symbolic", "恢复")
        self.restore_btn.set_tooltip_text("恢复上次删除")
        self.restore_btn.connect("clicked", self._on_restore)
        self.delete_btn = _button("user-trash-symbolic", "删除")
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.connect("clicked", self._on_delete)
        actions = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        actions.append(self.restore_btn)
        actions.append(self.delete_btn)
        preview_footer = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        preview_footer.add_css_class("pane-footer")
        preview_footer.append(self.status)
        preview_footer.append(actions)
        preview_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        preview_column.append(self.preview_stack)
        preview_column.append(Gtk.Separator())
        preview_column.append(preview_footer)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, vexpand=True)
        paned.set_shrink_start_child(False)
        paned.set_resize_start_child(False)
        paned.set_start_child(sidebar)
        paned.set_end_child(preview_column)
        paned.set_position(400)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.banner)
        body.append(paned)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(body)

        self.toast_overlay = Adw.ToastOverlay(child=toolbar)
        self.set_content(self.toast_overlay)
        self._sync_buttons()
        GLib.idle_add(self.refresh)

    def _install_style(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_string(STYLE)
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def refresh(self) -> bool:
        if self._busy:
            return False
        self._busy = True
        self._sync_buttons()
        self.window_title.set_subtitle("正在读取")
        self.status.set_text("正在读取会话…")
        threading.Thread(target=self._load_worker, daemon=True).start()
        return False

    def _load_worker(self) -> None:
        layout: Layout | None = None
        chats: list[Chat] = []
        error = ""
        label = ""
        try:
            layout = discover()
            label = f"{db_size_label(layout.global_db)}"
            chats = list_chats(layout)
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
        running = False
        try:
            running = cursor_is_running()
        except OSError:
            running = False
        GLib.idle_add(self._apply_load, layout, chats, running, label, error)

    def _apply_load(self, layout: Layout | None, chats: list[Chat], running: bool, label: str, error: str) -> bool:
        self.layout = layout
        self.chats = chats
        self.by_id = {chat.composer_id: chat for chat in chats}
        self._running = running
        self._load_error = error
        self._busy = False
        if error:
            self.window_title.set_subtitle("没能读取数据库")
            self.window_title.set_tooltip_text(error)
            self._show_placeholder("没能读取数据库", error, "dialog-warning-symbolic")
        else:
            self.window_title.set_subtitle(f"{len(chats)} 条 · {label}" if label else f"{len(chats)} 条")
            if layout is not None:
                self.window_title.set_tooltip_text(str(layout.global_db))
            self._show_placeholder(
                "选择一条会话",
                "点左侧的一条对话，这里会显示正文。\n按住 Ctrl 可以多选，再一起删除。",
            )
        self._refill()
        self._sync_banner()
        self._sync_buttons()
        self._update_status()
        return False

    def _refill(self) -> None:
        self.store.remove_all()
        for chat in self.chats:
            if not self._visible(chat):
                continue
            self.store.append(
                ChatObject(
                    composer_id=chat.composer_id,
                    heading=display_title(chat),
                    detail=self._detail(chat),
                    badge=self._badge(chat),
                    muted=chat.archived and not chat.subagent,
                )
            )

    def _visible(self, chat: Chat) -> bool:
        if chat.subagent and not self.show_subagents:
            return False
        if self.mode == "active" and chat.archived:
            return False
        if self.mode == "archived" and not chat.archived:
            return False
        query = self.query.strip().casefold()
        if not query:
            return True
        haystack = " ".join(
            (chat.title, chat.subtitle, chat.project, chat.project_path, chat.composer_id)
        ).casefold()
        return query in haystack

    def _detail(self, chat: Chat) -> str:
        when = format_time(chat.updated_at)
        return " · ".join(part for part in (chat.project, when) if part)

    def _badge(self, chat: Chat) -> str:
        if chat.subagent:
            return "子会话"
        if not chat.archived:
            return "使用中"
        return ""

    def _on_mode(self, group: Adw.ToggleGroup, _param: GObject.ParamSpec) -> None:
        name = group.get_active_name()
        self.mode = name if name in ("all", "active", "archived") else "all"
        self._refill()
        self._update_status()

    def _on_subagents(self, switch: Gtk.Switch, _param: GObject.ParamSpec) -> None:
        self.show_subagents = switch.get_active()
        self._refill()
        self._update_status()

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self.query = entry.get_text()
        self._refill()
        self._update_status()

    def _setup_row(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        list_item.set_child(RowBox())

    def _bind_row(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        row = list_item.get_item()
        widget = list_item.get_child()
        if not isinstance(row, ChatObject) or not isinstance(widget, RowBox):
            return
        widget.heading.set_text(row.heading or "未命名")
        widget.detail.set_text(row.detail or "")
        widget.badge.set_text(row.badge or "")
        widget.badge.set_visible(bool(row.badge))
        if row.badge == "使用中":
            widget.badge.add_css_class("current")
        else:
            widget.badge.remove_css_class("current")
        if row.muted:
            widget.heading.add_css_class("muted-title")
        else:
            widget.heading.remove_css_class("muted-title")

    def _on_selection(self, _selection: Gtk.MultiSelection, _position: int, _n_items: int) -> None:
        rows = self._selected_objects()
        self._selection_count = len(rows)
        self._sync_buttons()
        self._update_status()
        if not rows or self.layout is None:
            self._show_placeholder(
                "选择一条会话",
                "点左侧的一条对话，这里会显示正文。\n按住 Ctrl 可以多选，再一起删除。",
            )
            return
        chat = self.by_id.get(rows[-1].composer_id)
        if chat is None:
            return
        self._show_chat(chat, len(rows))
        self._preview_gen += 1
        generation = self._preview_gen
        self._show_note("正在读取…")
        threading.Thread(target=self._preview_worker, args=(generation, chat), daemon=True).start()

    def _preview_worker(self, generation: int, chat: Chat) -> None:
        layout = self.layout
        if layout is None:
            return
        try:
            transcript = load_transcript(layout, chat.composer_id)
        except Exception as exc:
            transcript = Transcript((), note=f"读预览失败：{exc}")
        GLib.idle_add(self._apply_preview, generation, transcript)

    def _apply_preview(self, generation: int, transcript: Transcript) -> bool:
        if generation != self._preview_gen:
            return False
        _clear(self.transcript)
        if transcript.truncated:
            self.transcript.append(self._note_label("更早的消息没有展开，这里是最近的对话。"))
        if not transcript.messages:
            self.transcript.append(self._note_label(transcript.note or "这条会话没有可显示的正文。"))
        for message in transcript.messages:
            self.transcript.append(self._message_bubble(message.role, message.text))
        GLib.idle_add(self._scroll_transcript_end)
        return False

    def _show_placeholder(self, title: str, description: str, icon: str = "view-list-symbolic") -> None:
        self.empty_page.set_icon_name(icon)
        self.empty_page.set_title(title)
        self.empty_page.set_description(description)
        self.preview_stack.set_visible_child_name("empty")

    def _show_chat(self, chat: Chat, selected_count: int) -> None:
        self.preview_stack.set_visible_child_name("content")
        if selected_count > 1:
            self.preview_title.set_text(f"已选择 {selected_count} 条")
            self.preview_meta.set_text(f"正在预览最后一条：{display_title(chat)}")
        else:
            self.preview_title.set_text(display_title(chat))
            when = format_time(chat.updated_at)
            place = chat.project_path or chat.project
            meta = [part for part in (place, " · ".join(part for part in (when, chat.composer_id) if part)) if part]
            self.preview_meta.set_text("\n".join(meta))
        self._set_preview_badges(chat)

    def _set_preview_badges(self, chat: Chat) -> None:
        _clear(self.preview_badges)
        badges = []
        if chat.subagent:
            badges.append(("子会话", False))
        badges.append(("已归档" if chat.archived else "使用中", not chat.archived))
        for text, current in badges:
            label = Gtk.Label(label=text)
            label.add_css_class("caption")
            label.add_css_class("chat-badge")
            if current:
                label.add_css_class("current")
            self.preview_badges.append(label)
        self.preview_badges.set_visible(True)

    def _selected_objects(self) -> list[ChatObject]:
        bitset = self.selection.get_selection()
        rows: list[ChatObject] = []
        for index in range(bitset.get_size()):
            item = self.selection.get_item(bitset.get_nth(index))
            if isinstance(item, ChatObject):
                rows.append(item)
        return rows

    def _on_key(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType) -> bool:
        if keyval == Gdk.KEY_Delete:
            self._on_delete(self.delete_btn)
            return True
        return False

    def _on_delete(self, _button: Gtk.Button) -> None:
        if self._busy or self.layout is None:
            return
        selected = [chat.composer_id for chat in self._selected_chats()]
        if not selected:
            self._toast("先选中一条会话")
            return
        if self._running:
            self._toast("先完全退出 Cursor")
            return
        expanded = expand_with_children(self.chats, selected)
        self._busy = True
        self._sync_buttons()
        self.status.set_text("正在统计要删除的数据…")
        threading.Thread(target=self._count_worker, args=(expanded, selected), daemon=True).start()

    def _count_worker(self, expanded: list[str], selected: list[str]) -> None:
        layout = self.layout
        if layout is None:
            GLib.idle_add(self._ask_delete, expanded, selected, 0, "数据库还没准备好")
            return
        try:
            count = count_owned_keys(layout, expanded)
            error = ""
        except Exception as exc:
            count = 0
            error = str(exc) or exc.__class__.__name__
        GLib.idle_add(self._ask_delete, expanded, selected, count, error)

    def _ask_delete(self, expanded: list[str], selected: list[str], count: int, error: str) -> bool:
        self._busy = False
        self._sync_buttons()
        if error:
            self._toast(error)
            self._update_status()
            return False
        extra = max(0, len(expanded) - len(set(selected)))
        lines = [f"将删除 {len(expanded)} 条会话。"]
        if extra:
            lines.append(f"其中 {extra} 条是选中会话的子会话。")
        lines.append(f"关联数据约 {count} 行。")
        lines.append("删除前会先写到本机备份。")
        for composer_id in expanded[:8]:
            chat = self.by_id.get(composer_id)
            lines.append(display_title(chat) if chat else composer_id)
        if len(expanded) > 8:
            lines.append("……")
        dialog = Adw.AlertDialog(heading="删除所选会话", body="\n".join(lines))
        dialog.add_response("cancel", "取消")
        dialog.add_response("delete", "删除")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        self._pending_ids = list(expanded)
        dialog.connect("response", self._on_delete_response)
        dialog.present(self)
        return False

    def _on_delete_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        pending = self._pending_ids
        self._pending_ids = []
        if response != "delete" or self.layout is None or not pending:
            return
        self._busy = True
        self._sync_buttons()
        self.status.set_text("正在备份并删除…")
        threading.Thread(target=self._delete_worker, args=(pending,), daemon=True).start()

    def _delete_worker(self, composer_ids: list[str]) -> None:
        layout = self.layout
        if layout is None:
            GLib.idle_add(self._apply_delete, None, "数据库还没准备好")
            return
        try:
            report = delete_chats(layout, composer_ids)
            error = ""
        except Exception as exc:
            report = None
            error = str(exc) or exc.__class__.__name__
        GLib.idle_add(self._apply_delete, report, error)

    def _apply_delete(self, report: object, error: str) -> bool:
        self._busy = False
        if error or report is None:
            self._toast(error or "删除没有完成")
            self._sync_buttons()
            self._update_status()
            return False
        self._toast(f"已删除 {len(report.deleted_ids)} 条会话")
        if report.warnings:
            self._toast("；".join(report.warnings[:3]))
        self.status.set_text(f"备份在 {report.backup_dir}")
        self.refresh()
        return False

    def _on_restore(self, _button: Gtk.Button) -> None:
        if self._busy or self.layout is None:
            return
        if self._running:
            self._toast("先完全退出 Cursor")
            return
        backup = latest_backup(self.layout)
        if backup is None:
            self._toast("没有可恢复的备份")
            return
        dialog = Adw.AlertDialog(
            heading="恢复上次删除",
            body=f"把最近一次备份写回数据库。\n{backup}",
        )
        dialog.add_response("cancel", "取消")
        dialog.add_response("restore", "恢复")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_restore_response)
        dialog.present(self)

    def _on_restore_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "restore" or self.layout is None:
            return
        self._busy = True
        self._sync_buttons()
        self.status.set_text("正在恢复…")
        threading.Thread(target=self._restore_worker, daemon=True).start()

    def _restore_worker(self) -> None:
        layout = self.layout
        if layout is None:
            GLib.idle_add(self._apply_restore, "数据库还没准备好")
            return
        try:
            report = restore_latest(layout)
            message = f"已恢复 {report.header_rows} 条会话头、{report.kv_rows} 行数据"
            if report.warnings:
                message += "。" + "；".join(report.warnings[:2])
            error = ""
        except Exception as exc:
            message = ""
            error = str(exc) or exc.__class__.__name__
        GLib.idle_add(self._apply_restore, error or message, not error)

    def _apply_restore(self, message: str, ok: bool = False) -> bool:
        self._busy = False
        self._toast(message)
        self._sync_buttons()
        if ok:
            self.refresh()
        else:
            self._update_status()
        return False

    def _sync_banner(self) -> None:
        if self._running:
            self.banner.set_title("Cursor 正在运行。先完全退出，再删除或恢复。")
            self.banner.set_revealed(True)
        elif self._load_error:
            self.banner.set_title(self._load_error)
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def _sync_buttons(self) -> None:
        can_write = self.layout is not None and not self._busy and not self._running
        self.delete_btn.set_sensitive(can_write and self._selection_count > 0)
        self.restore_btn.set_sensitive(can_write)
        self.refresh_btn.set_sensitive(not self._busy)
        self.mode_group.set_sensitive(not self._busy)
        self.search.set_sensitive(not self._busy)
        self.sub_switch.set_sensitive(not self._busy)

    def _update_status(self) -> None:
        if self._busy:
            return
        shown = self.store.get_n_items()
        total = len(self.chats)
        self.count_label.set_text(f"{shown} / {total}")
        if self._selection_count:
            self.status.set_text(f"已选 {self._selection_count} 条")
        else:
            self.status.set_text("")

    def _selected_chats(self) -> list[Chat]:
        chosen: list[Chat] = []
        for item in self._selected_objects():
            chat = self.by_id.get(item.composer_id)
            if chat is not None:
                chosen.append(chat)
        return chosen

    def _show_note(self, text: str) -> None:
        _clear(self.transcript)
        self.transcript.append(self._note_label(text))

    def _note_label(self, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0, wrap=True)
        label.add_css_class("dim-label")
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        return label

    def _message_bubble(self, role: str, text: str) -> Gtk.Widget:
        caption = Gtk.Label(xalign=0)
        caption.add_css_class("caption")
        caption.add_css_class("who")
        body = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True, max_width_chars=48)
        body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.add_css_class("msg")
        bubble.append(caption)
        bubble.append(body)
        if role == "user":
            caption.set_text("你")
            bubble.add_css_class("msg-user")
            bubble.set_halign(Gtk.Align.END)
            bubble.set_margin_start(72)
        else:
            caption.set_text("Cursor")
            bubble.add_css_class("msg-cursor")
            bubble.set_halign(Gtk.Align.START)
            bubble.set_margin_end(72)
        return bubble

    def _scroll_transcript_end(self) -> bool:
        adjustment = self.transcript_scroll.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        return False

    def _toast(self, message: str) -> None:
        if message:
            self.toast_overlay.add_toast(Adw.Toast.new(message))


class DeskApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="dev.cursorchatdesk.Desk", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connect("activate", self._activate)

    def _activate(self, _app: Adw.Application) -> None:
        window = self.props.active_window
        if window is None:
            window = DeskWindow(self)
        window.present()


def main() -> None:
    app = DeskApp()
    raise SystemExit(app.run(sys.argv))
