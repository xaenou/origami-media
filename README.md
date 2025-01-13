# Origami Media

The purpose of this maubot plugin is to process and display media for matrix servers when a valid url is detected or when the appropriate command is invoked.

## Features

Here are some of the current features:

- **Dynamic Thumbnail Generation**: Thumbnails can be automatically generated for media that lacks them.  
- **Configurable Concurrency**: Concurrency levels at various stages of the pipeline can be tweaked depending on your server's demands and resources.  
- **Event Queue**: Validated commands can be buffered for processing to prevent overwhelming the server. Works hand-in-hand with the concurrency.  
- **Batch Processing**: If multiple urls are detected in a single post, they can all be processed.
- **UX & Accessibility**: Everything looks good and works just as well for different matrix clients. Helpful reactions appear to let users know that the bot is processing their requests. 
- **Various Constraints**: Set limits on file sizes in and out of memory, video duration limits, and more to manage your server's resources.  
- **Custom Command Prefixes**: You don't have to use `!`.  
- **Advanced YTDLP Configuration**: Configure cookies, proxies, user agents, preferred formats, and fallback formats for any platform you decide to add.  
- **Livestream Previews**: Generate previews for live streams via FFmpeg. The duration of the preview can be set. 
- **Link Censorship and Sanitization**: Automatically censor and sanitize tracking links posted by users.  
- **Media Standardization**: Convert media (e.g., WebM files to MP4) automatically for compatibility between different clients.  
- **Configurable Whitelist**: Set up a customizable whitelist for specific use cases.  

...and more! All features are configurable in real time through the **Maubot Manager Dashboard** by editing the assigned instance's `base-config.yaml`.

## Commands

A basic overview of the current commands supported:
- `!help`: Show this help message.
- `!get [url]`: Download media from a url.
- `!audio [url]`: Download audio only for a url. (Aliases: mp3)
- `!tenor [query]`: Download gif by querying tenor. (Aliases: gif)
- `!unsplash [query]`: Download image by querying unsplash. (Aliases: img)
- `!lexica [query]`: Download an image by querying Lexica. (Aliases: lex)
- `!waifu`: Roll for a random Waifu. (Aliases: girl, g)

**Note**: If passive URL detection is enabled, incoming messages are parsed for URLs and the `get` command is applied to them.

## Dependencies

- Maubot
- yt-dlp cli
- ffmpeg cli

## Planned features

- See issues
