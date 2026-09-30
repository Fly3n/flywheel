# wx-fetch

专门用于抓取并解析微信公众号文章内容（`https://mp.weixin.qq.com/s/...`）的轻量级工具与 Skill，规避普通爬虫防刷拦截与本地代理 SSL 冲突，提取标题、作者、发布时间和清洗后的正文。

## 解决什么问题

直接抓取微信公众号文章通常面临两大障碍：
1. **反爬机制拦截**：普通 HTTP 请求容易触发微信反爬防护，返回“环境异常，去验证”页面。
2. **本地代理冲突**：本地运行代理客户端时，抓取微信页面常遭遇 SSL 握手异常（`SSL: UNEXPECTED_EOF_WHILE_READING`）。

`wx-fetch` 通过内置确定性脚本，提供稳定的免配抓取体验。

## 核心机制

1. **客户端 UA 伪装**：模拟 iOS 微信内置浏览器（MicroMessenger）发起请求，绕过网页反爬验证。
2. **代理环境隔离**：请求期间主动剥离环境变量中的 `http_proxy`/`https_proxy`，避开本地代理造成的 SSL 握手断开。
3. **精准 DOM 清洗与解密**：定位 `activity-name`、`js_name` 与 `js_content` 节点，剔除 script、style 与内联修饰标签，将正文清洗为 Markdown 友好的段落纯文本。
4. **URL 本地持久化缓存**：默认基于 URL 的 SHA-256 哈希持久化缓存完整正文，避免对相同链接重复发起请求。

## 使用方式

依赖 Python 3.8+ 标准库，无需额外安装第三方依赖。

把本 Skill 目录作为工作目录运行脚本：

```bash
# 默认优先读取缓存；未命中时抓取并自动缓存完整正文
python scripts/fetch_wechat_article.py "<URL>"

# 输出 JSON 格式并额外导出到指定文件
python scripts/fetch_wechat_article.py "<URL>" --json -o article.json

# 忽略缓存并重新抓取
python scripts/fetch_wechat_article.py "<URL>" --refresh

# 指定自定义缓存目录
python scripts/fetch_wechat_article.py "<URL>" --cache-dir /path/to/custom_cache
```

## 个性化清单

| 等级 | 建议修改项 | 说明 |
| --- | --- | --- |
| P1 | 默认缓存目录 | 默认缓存在 `~/.wx-fetch/cache/articles/`。如果已有统一的集中式文章缓存规范，可修改脚本中的 `DEFAULT_CACHE_DIR` 或通过 `--cache-dir` 传入 |
