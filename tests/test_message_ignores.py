import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import discord2tg as control
import discord2tg_common as common


class MessageIgnoresTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"watch_servers": ["Guild"]}
        self.patches = [
            patch.object(common, "load_cfg", side_effect=lambda: self.cfg),
            patch.object(control, "load_cfg", side_effect=lambda: self.cfg),
            patch.object(control, "save_cfg"),
            patch.object(control, "load_state", return_value={"known_channels": {"Guild/#general": 10}}),
            patch.object(control, "remember_label"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def forward(self, text="hello", channel="general", ids=(10,), **kwargs):
        return common.should_forward("Guild", channel, "bot", text, ids=ids, **kwargs)

    def test_scoped_text_leaves_other_channels_and_messages(self):
        control.modify_message_ignore("/ignore_text", "10 | Messages Sent:")
        self.assertEqual(self.forward("MESSAGES SENT: today 14"), (False, False))
        self.assertEqual(self.forward("Messages Sent:", "other", (20,)), (True, False))
        self.assertEqual(self.forward(), (True, False))

    def test_embeds_fields_and_forward_integration(self):
        control.modify_message_ignore("/ignore_text", "10 | Messages Sent:")
        message = NS(id=100, content="", clean_content="", author=NS(display_name="bot", name="bot", id=1),
                     embeds=[NS(title=None, description=None, fields=[NS(name="Messages Sent:", value="Today: 14")])])
        with patch.object(common, "load_state", return_value={"admin_chat": "1"}), patch.object(common, "tg_send") as send:
            common.forward(message, "Guild", "general", ids=(10,))
        send.assert_not_called()
        self.assertEqual(self.forward(embed_text=common.message_embed_text(message)), (False, False))

    def test_name_and_thread_parent_match(self):
        self.cfg["ignore_text"] = [{"channel": "Guild/general", "text": "warning"}]
        self.assertEqual(self.forward("warning"), (False, False))
        self.assertEqual(self.forward("warning", "thread", (30, 10), parent="general"), (False, False))
        self.cfg["ignore_text"][0]["channel"] = "10"
        self.assertEqual(self.forward("warning", "thread", (30, 10), parent="general"), (False, False))

    def test_explicit_ignores_override_follow_and_notify(self):
        self.cfg.update(follow_users=["bot"], notify_users=["bot"], ignore_messages=[100])
        self.assertEqual(self.forward(message_id=100), (False, False))
        self.assertEqual(self.forward(message_id=101), (True, True))
        self.cfg["ignore_text"] = [{"channel": "10", "text": "hello"}]
        self.assertEqual(self.forward(message_id=101), (False, False))

    def test_message_link_only_blocks_one_id_and_can_be_removed(self):
        link = "https://discord.com/channels/1/10/100"
        control.modify_message_ignore("/ignore_message", link)
        control.modify_message_ignore("/ignore_message", "100")
        self.assertEqual(self.cfg["ignore_messages"], ["100"])
        self.assertEqual(self.forward(message_id=100), (False, False))
        self.assertEqual(self.forward(message_id=101), (True, False))
        control.modify_message_ignore("/unignore_message", link)
        self.assertEqual(self.forward(message_id=100), (True, False))

    def test_text_dedup_list_remove_and_validation(self):
        control.modify_message_ignore("/ignore_text", "Guild/#general | warning")
        control.modify_message_ignore("/ignore_text", "10 | WARNING")
        self.assertEqual(len(self.cfg["ignore_text"]), 1)
        self.assertIn("warning", control.modify_message_ignore("/ignores", ""))
        control.modify_message_ignore("/unignore_text", "10 | Warning")
        self.assertEqual(self.cfg["ignore_text"], [])
        for cmd, arg in [("/ignore_text", "10 | "), ("/ignore_text", "| x"),
                         ("/ignore_message", "https://discord.com/channels/1/10")]:
            self.assertIn("Usage:", control.modify_message_ignore(cmd, arg))

    def test_old_filters_and_empty_rule(self):
        self.assertEqual(self.forward(), (True, False))
        self.cfg["ignore_text"] = [{"channel": "10", "text": ""}]
        self.assertEqual(self.forward(), (True, False))
        self.cfg["search_exclude"] = ["hello"]
        self.assertEqual(self.forward(), (False, False))
        self.cfg["follow_users"] = ["bot"]
        self.assertEqual(self.forward(), (True, False))
        self.cfg["ignore_channels"] = ["10"]
        self.assertEqual(self.forward(), (False, False))

    def test_button_preview_confirm_and_cancel(self):
        state = {"admin_chat": "1"}
        def update(**values):
            state.update(values)
            return state
        cb = {"message": {"text": "Bot (Guild/#general)\nreply to User: g.m\n⚠️ **Prefix commands will be removed on <t:1795996800:D>\nTry /messages"}}
        with patch.object(control, "load_state", side_effect=lambda: state), patch.object(control, "update_state", side_effect=update), patch.object(control, "tg_send"), patch.object(control, "show"):
            control._callback(cb, "1", 55, "msgignore:10")
            pending = state["wait"].copy()
            self.assertEqual(pending["text"], "⚠️ **Prefix commands will be removed on")
            self.assertNotIn("ignore_text", self.cfg)
            control._callback({}, "1", 56, "msgignore_ok:stale")
            self.assertNotIn("ignore_text", self.cfg)
            control._callback({}, "2", 56, "msgignore_ok:" + pending["token"])
            self.assertNotIn("ignore_text", self.cfg)
            control._callback({}, "1", 56, "msgignore_ok:" + pending["token"])
            self.assertEqual(self.forward(cb["message"]["text"]), (False, False))
            self.assertEqual(self.forward("ordinary chat"), (True, False))
            control._callback({"message": {"text": "Bot (Guild/#general)\n> Messages Sent:\n\nToday: **9**"}}, "1", 57, "msgignore:10")
            self.assertEqual(state["wait"]["text"], "Messages Sent:")
            control._callback({}, "1", 58, "msgignore_cancel:" + state["wait"]["token"])
            self.assertEqual(len(self.cfg["ignore_text"]), 1)

    def test_custom_fragment_from_button(self):
        reply = unittest.mock.Mock()
        control.handle_wait({"op": "ignore_text", "cid": "10"}, "Messages Sent:", "1", reply)
        self.assertEqual(self.forward(embed_text="Messages Sent:\nToday: **31**"), (False, False))
        self.assertEqual(self.forward("Messages Sent:", "other", (20,)), (True, False))
        reply.assert_called_once()

    def test_forward_includes_source_channel_button(self):
        message = NS(id=100, content="hello", clean_content="hello", embeds=[],
                     channel=NS(id=10), author=NS(display_name="bot", name="bot", id=1))
        with patch.object(common, "load_state", return_value={"admin_chat": "1"}), patch.object(common, "format_message", return_value="Bot (Alias)\nhello"), patch.object(common, "tg_send") as send:
            common.forward(message, "Guild", "general", ids=(10,))
        self.assertEqual(send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "msgignore:10")


if __name__ == "__main__":
    unittest.main()
