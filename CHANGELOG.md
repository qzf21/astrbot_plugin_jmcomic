# Changelog

## v1.1.0

- 新增 `/jm <关键词...>` 关键词随机推荐，空格分隔多个关键词。
- 默认要求所有关键词都命中标题/标签/作者；支持 `-关键词` 排除。
- 新增 `keyword_search_pages`、`keyword_search_attempts`、`keyword_match_all` 配置。
- 更新帮助文本、README 和示例。

## v1.0.1

- 更换插件图标为新的 256×256 Logo。
- README 补充 NapCat / 1Panel 的文件目录挂载与 PDF 发送说明。
- 补充 `callback_api_base`、权限、`NAPCAT_UID/GID` 和大文件超时的排查说明。

## v1.0.0

- 首个版本：
  - `/jm <ID>` 查询封面、作者、标签、简介；
  - `/jm 下载 <ID>` 下载整本并合并为 PDF 发送；
  - `/jm 推荐` 随机推荐；
  - 支持 PDF 分卷、缓存、群聊权限、标签过滤、代理与自定义域名。
