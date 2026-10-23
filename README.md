# SakuraMedia 影片文案抓取与翻译

从 DMM 抓取影片的日文标题和简介，可选翻译成中文后写回影片的 `title` 和 `summary`。

## 配置

不翻译时使用默认配置即可，此时写回 DMM 日文原文。需要翻译时在插件配置表单中填写并开启「启用翻译」，模型建议使用 ollama cloud的gemma4:31b模型， API类型为chat_completions

## 任务

- `sakuramedia_movie_scrape_translate_sync_subscribed`：每日 04:10 自动全量同步。
- `sakuramedia_movie_scrape_translate_sync`：手动处理指定番号。


## 常见问题
1. 插件内部会自动跳过符合以下条件的影片
番号为 `FC2-` 开头、纯数字分隔番号（如 `123-456`）和 2015 年前的影片；
2. 会处理哪些影片？
优先处理已订阅影片和含订阅女优的影片，其次是未订阅影片。
3. 如何配置dmm代理?
dmm网站需要日本IP可以访问， 在部署`sakuramedia`时给docker容器设置代理，且自行做好分流。
## License

本项目采用 [PolyForm Noncommercial License 1.0.0](./LICENSE)，仅允许非商业用途。

Copyright (C) 2026 tinypinglite
