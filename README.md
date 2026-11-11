# Discord to Telegram bridge

## Ignore messages in one channel

New forwarded messages include an **Ignore similar** button. Press it,
check the suggested text, then confirm **Ignore**.
You can send your own text fragment instead, or cancel. For statistics embeds,
the suggestion is `Messages Sent:`; for the warning it uses the text before the
Discord timestamp. Counts and other text can change without affecting a match.
The channel itself remains enabled. Older messages without buttons can still
be filtered using the commands below.

Send these commands to the Telegram control bot as an admin. Replace `CHANNEL_ID`
with the Discord channel ID, channel link, or `Server/#channel` name. IDs survive
channel renames; `/channels` lists known channel names.

For the statistics and command deprecation notices shown in the example:

```text
/ignore_text CHANNEL_ID | Messages Sent:
/ignore_text CHANNEL_ID | Prefix commands will be removed
```

These are case-insensitive literal substrings, matched against the message body
and embed titles, descriptions and fields. Only the selected channel (and its
threads, when the parent channel is selected) is affected. Other messages in that
channel and matching text in other channels still pass the normal filters.
Reply previews, author names and Telegram headers are not searched.

To ignore just one Discord message, copy its message link:

```text
/ignore_message https://discord.com/channels/SERVER_ID/CHANNEL_ID/MESSAGE_ID
```

Use a real Discord link with numeric IDs; a numeric message ID is also accepted.
This matches only that ID, not future messages with the same text, and does not
delete copies already sent to Telegram.

```text
/ignores
/unignore_text CHANNEL_ID | Messages Sent:
/unignore_text CHANNEL_ID | Prefix commands will be removed
/unignore_message MESSAGE_ID
```

Rules apply immediately in both bridge modes, including followed users and
notification messages. Existing configurations remain compatible. Rules are
stored in `ignore_text` (objects with `channel` and `text`) and `ignore_messages`
(message IDs) in `discord2tg_config.json`. Reset filters clears these rules too.
`/ignore CHANNEL_ID` still ignores the entire channel.

Run offline regression checks with `python3 -m unittest discover -s tests -v`.
