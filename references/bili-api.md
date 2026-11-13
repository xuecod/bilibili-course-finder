# B 站接口备忘

全部在 2026-11 实测通过。B 站接口随时可能变，出问题先看这里。

## 1 wbi 签名（搜索/摘要等接口必需）

搜索接口不带签名返回 `-412`。三步：

```python
# 1) 拿设备指纹（避免被判无 cookie）
spi = GET https://api.bilibili.com/x/frontend/finger/spi
cookie = "buvid3=%s; buvid4=%s" % (spi["data"]["b_3"], spi["data"]["b_4"])

# 2) 从 nav 拿 wbi key
nav = GET https://api.bilibili.com/x/web-interface/nav   # 带上面的 cookie
img_url = nav["data"]["wbi_img"]["img_url"]   # 取文件名去掉 .png
sub_url = nav["data"]["wbi_img"]["sub_url"]
raw = img_key + sub_key                        # 64 字符

# 3) 按置换表重排取前 32 位 = mixin_key
mixin_key = "".join(raw[i] for i in MIXIN_TAB)[:32]

# 4) 签名：参数按 key 排序 -> urlencode -> 过滤 !'()* -> 拼 mixin_key -> md5
params["wts"] = int(time.time())
qs = urlencode(sorted(params.items()), quote_via=quote)
qs = "".join(c for c in qs if c not in "!'()*")
params["w_rid"] = md5((qs + mixin_key).encode()).hexdigest()
```

置换表（`MIXIN_TAB`，硬编码在 `scripts/bili.py`）：

```
46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,
12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,
57,62,11,36,20,34,44,52
```

`mixin_key` 有效期约 10 分钟，脚本缓存在系统临时目录 `bili_course_cache.json`。

## 2 接口清单

| 用途 | 接口 | 签名 | 登录 |
|---|---|---|---|
| 搜索视频 | `GET /x/web-interface/wbi/search/type` | 要 | 否 |
| 视频详情 | `GET /x/web-interface/view?bvid=` | 否 | 否 |
| 弹幕 XML | `GET https://comment.bilibili.com/{cid}.xml` | 否 | 否 |
| 一级评论 | `GET /x/v2/reply?type=1&oid={aid}&sort=2&ps=20&pn=1` | 否 | 否 |
| 字幕轨道 | `GET /x/player/v2?aid=&cid=` | 否 | **是** |
| AI 摘要 | `POST /x/web-interface/view/conclusion/get` | 要 | **是** |

搜索必需参数：`search_type=video&keyword=&page=&page_size=&order=&platform=pc&web_location=1430654&from_source=webtop_search`

`order` 可选：`totalrank`（综合）/ `click`（播放）/ `pubdate` / `dm`（弹幕）/ `stow`（收藏）

## 3 风控与报错

| code | 含义 | 处理 |
|---|---|---|
| `0` | 正常 | — |
| `-412` | 请求被拦截 | **多半是请求头不全**，不是封 IP。补 `Origin`、`Sec-Fetch-Site/Mode/Dest`、`Accept-Language` 后重试 |
| `-352` | 风控校验失败 | 刷新 cookie 与 wbi key 后重试 |
| `-401` | 未登录 | 需要 `SESSDATA` |
| `-101` | 账号未登录 | 字幕 / AI 摘要必须登录 |
| `-404` | 视频不存在 | 跳过 |

## 4 踩过的坑（都是实测翻车后记的）

**① 关键词带空格 → 返回 0 条**
`keyword=汇编语言 王爽` 无论编码成 `+` 还是 `%20` 都是 0 条，`汇编语言王爽` 才有结果。
所有关键词在发请求前必须 `re.sub(r"\s+", "", kw)`。
也意味着不能靠长尾词提高精度，只能靠**多组无空格变体**做召回。

**② 搜索接口的 duration 是 MM:SS，且不含分P数**
`"293:4"` = 293 分 4 秒（不是「293 分钟 4 个分P」），`"8:27"` = 8 分 27 秒，分钟位可以超过 59。
最坑的地方：它**根本没有分P字段**。曾误判成「分钟:分P数」，导致 `8:27` 被解析成 27 个分P，
单个做菜视频的"结构分"直接拿了满分。分P 数只能在详情接口的 `pages` 长度里拿。
详情接口的 `duration`、`pages[].duration` 单位是**秒**。

**③ 弹幕 XML 是 raw deflate 压缩流**
直接 `decode("utf-8")` 得到乱码、`<d p=` 计数为 0。
必须 `zlib.decompressobj(-15).decompress(data)`（先判断 gzip 魔数 `\x1f\x8b`）。
文章正文里也带弹幕文本，解析时只取 `<d p="...">...</d>`。

**④ 缺浏览器指纹头 → 412**
只带 `User-Agent` + `Referer` 会被 `view` 接口拒；补上 `Origin`、`Sec-Fetch-Site`、`Sec-Fetch-Mode`、`Sec-Fetch-Dest`、`Accept-Language` 即通过。所以 412 优先怀疑请求头而不是限流。

**⑤ 标题里有高亮标签**
搜索结果的 `title` 含 `<em class="keyword">...</em>`，还有 `&quot;` 等 HTML 实体，入库前必须清洗。

**⑥ 部分 bvid 详情返回空**
短视频、番剧、已失效视频会返回 `code != 0` 或 `data` 为 null，要能跳过而不是崩掉。

**⑦ 评论新旧两套接口**
旧的 `/x/v2/reply` 仍然可用且不需要签名，返回结构简单，够用；新的 `/x/v2/reply/wbi/main` 要签名且分页参数是 JSON 字符串，没必要。

## 5 想解锁字幕 / AI 摘要

两者都要登录态。让用户自己取 cookie（不要代劳，涉及账号安全）：

1. 浏览器登录 B 站 → F12 → Application → Cookies → `https://www.bilibili.com`
2. 复制 `SESSDATA` 的值
3. `set BILI_SESSDATA=xxxx`（或写成用户环境变量）
4. 脚本会自动拼进 cookie，`probe` 时附带字幕

有登录态后 `subtitle()` 能拿到 `ai-zh`（B 站 AI 自动字幕）或 UP 上传的 CC 字幕，返回 `[{t, text}]`。
