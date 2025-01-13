# Origami Media

The purpose of this maubot plugin is to process and display media for matrix servers when a valid url is detected or when the appropriate command is invoked.

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

## Features

Here are some of the current features available:

- **Advanced YTDLP Configuration**  
  Configure cookies, proxies, user agents, preferred formats, and fallback formats for any platform added. 
- **Dynamic Thumbnail Generation**    
  Thumbnails can be automatically created for media that lacks them.
- **Livestream Previews**  
  Livestreams can be detected and handled by generating a preview of it via ffmpeg. The duration of the preview can be configured
- **Batch Processing**   
  Automatically process multiple URLs detected in a single post. Can be disabled.
- **Configurable Constraints**  
  Set limits on file size, video duration, and other parameters to optimize server resource usage.  
- **Thumbnail Fallback**  
  If a requested video violates preset constraints (e.g., max file size), its thumbnail can be posted with useful information.  
- **Media Standardization**  
  Convert media formats (e.g., WebM to MP4) for compatibility across different clients.
- **Whitelist**  
  Add different platforms that should be processed.
- **Custom Command Prefixes**  
  Use any prefix, not just `!`.
- **Tweakable Concurrency**   
  Concurrency levels at various stages of the pipeline can be tweaked depending on the server's demands and resources.
- **Event Queue**  
  Validated commands can be buffered for processing to prevent overwhelming the server. Works hand-in-hand with the concurrency. 
- **UX & Accessibility**  
  Everything looks good and works well for different matrix clients. Helpful reactions appear to let users know that the bot is processing their requests. 
- **Tracking Link Sanitization**  
  Automatically replace tracking links sent by users.
- **Query APIs**   
  API keys can be added for platforms like tenor, and then users can send queries to retrieve media such as gifs.
- **Disable prefixed commands or automatic URL detection**  
  Maybe automatic URL detection in the chat isn't desired, or prefixed commands are not being used.

All features are configurable in real time through the **Maubot Manager Dashboard** by editing the assigned instance's `base-config.yaml`.

## Dependencies

- Maubot
- yt-dlp cli
- ffmpeg cli

## Planned features

- See issues
