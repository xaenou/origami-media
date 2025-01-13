# Origami Media

The purpose of this maubot plugin is to process and display media for matrix servers when a valid url is detected or when the appropriate command is invoked.

## Features

A brief list of some of the current features:
- Dynamic thumbnail generation
- Configurable concurrency
- An event queue
- Batch processing
- File size constraints
- Custom command prefixes
- Configurable cookies, proxies, custom user agents, preferred format, and fallback formats for ytdlp per platform
- Livestream previews via ffmpeg
- Censor tracking links posted by users
- Media standardization (e.g. webms can be auto converted to mp4s)

and more, all configurable in real time via the maubot manager dashboard.

## Commands

A basic overview of the current commands supported:
- `!help`: Show this help message.
- `!get [url]`: Download media from a url.
- `!audio [url]`: Download audio only for a url. (Aliases: mp3)
- `!tenor [query]`: Download gif by querying tenor. (Aliases: gif)
- `!unsplash [query]`: Download image by querying unsplash. (Aliases: img)
- `!lexica [query]`: Download an image by querying Lexica. (Aliases: lex)
- `!waifu`: Roll for a random Waifu. (Aliases: girl, g)

Note: If passive url detection is enabled it applies the get command to whitelisted urls, and the get command will no longer appear in !help.
Note: If debug mode is enabled, additional commands may appear.

## Dependencies

- Maubot
- yt-dlp cli
- ffmpeg cli

## Planned features

- See issues
