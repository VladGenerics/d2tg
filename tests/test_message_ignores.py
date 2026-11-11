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


if __name__ == "__main__":
    unittest.main()
