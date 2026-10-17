# smartedu-textbook

国家中小学智慧教育平台（[basic.smartedu.cn](https://basic.smartedu.cn)）教材与听力资源的**检索 / 下载**工具。

- 全库索引 **3,565 条**资源（教材、信息科技教学指南、专题课程），本地缓存后毫秒级检索。
- 三条下载通道：原版矢量 PDF（离线算签名）、免登录切片 PDF（零 Token 兜底）、配套听力音频。
- 仅依赖 **Python 3 标准库**，无第三方包，也不需要写 skill 目录。

面向 AI Agent 的精简说明见 [`SKILL.md`](SKILL.md)；本文档面向人类用户，包含原理、参数、实测数据与排错细节。

---

## 目录

- [快速开始](#快速开始)
- [命令参考](#命令参考)
- [工作原理](#工作原理)
- [两条 PDF 通道对比](#两条-pdf-通道对比)
- [签名算法详解](#签名算法详解)
- [切片合成详解](#切片合成详解)
- [缓存机制](#缓存机制)
- [实测数据](#实测数据)
- [排错手册](#排错手册)
- [已知限制](#已知限制)
- [项目结构](#项目结构)

---

## 快速开始

```bash
# 1. 环境要求：Python 3.8+，无需 pip install
python3 --version

# 2. 自检：Token 在哪、是否过期、缓存目录是否可写、接口是否通
python3 scripts/download_smartedu.py doctor --verify

# 3. 检索教材，拿到 contentId
python3 scripts/download_smartedu.py search 小学 英语 教科版 五年级

# 4A. 免登录下载（推荐先用这个验证链路是否通畅）
python3 scripts/download_smartedu.py download 27eb5941-f566-431a-a2a6-92e45a2995e1 \
    --mode slices -o english_5a_slices.pdf

# 4B. 有 UC_TOKEN 时下载原版矢量 PDF（体积更小、缩放更清晰）
export UC_TOKEN='<从浏览器复制>'
python3 scripts/download_smartedu.py download 27eb5941-f566-431a-a2a6-92e45a2995e1 \
    -o english_5a.pdf

# 5. 配套听力（每曲一份，格式自动回退）
python3 scripts/download_smartedu.py audio 27eb5941-f566-431a-a2a6-92e45a2995e1 -o ./english_5a_audios
```

所有命令都是幂等的：重复执行只会覆盖同名输出、复用已有缓存，不会重复拉取全库索引。

---

## 命令参考

### `search` — 检索资源

```bash
python3 scripts/download_smartedu.py search <关键词...> [--limit N] [--json] [--refresh]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `keywords` | 必填 | 一个或多个关键词，**全部命中（AND）** 才算匹配 |
| `--limit` | `20` | 最多展示条数 |
| `--json` | 关 | 输出 `[{id, title, breadcrumbs, kind}]`，便于管道处理 |
| `--refresh` | 关 | 忽略本地版本缓存；但只有在远端 `module_version` 变化时才真正重拉分片 |

匹配范围为 `title + breadcrumbs` 的小写子串。关键词内部如有空格会被拆成多个词，因此 `小学 英语` 与 `"小学 英语"`（一个参数）等价。

**输出示例**

```text
检索到 2 条符合条件的资源:

序号   Content ID                             教材名称与分类
------------------------------------------------------------------------------------------
1    27eb5941-f566-431a-a2a6-92e45a2995e1   （根据2022年版课程标准修订）义务教育教科书·英语 五年级上册
     └─ 分类: 电子教材 > 小学 > 英语 > 教科版（主编：龚亚夫） > 五年级 > 上册
2    285ae1ca-33f3-40b5-b9c8-c6fd8256168b   义务教育教科书·英语（三年级起点）五年级下册
     └─ 分类: 电子教材 > 小学 > 英语 > 教科版（主编：龚亚夫） > 五年级 > 下册
```

**检索技巧**

- 书名里常带「（根据2022年版课程标准修订）」等前缀，只搜书名主体更容易命中。
- 想按学段/学科/版本精确筛选，直接叠加词：`小学 数学 人教版 三年级 上册`。
- 结果 `分类` 为空时，说明是平台未收录分类的教学指南/课件类资源（见[已知限制](#已知限制)）。
- 换关键词不会触发索引刷新，索引只在版本号变化时重新拉取，所以反复检索很快。

### `download` — 下载教材 PDF

```bash
python3 scripts/download_smartedu.py download <contentId> [-o 输出路径] \
    [--mode vector|slices] [--token TOKEN] [--token-from-shell]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `id` | 必填 | `search` 输出的 Content ID |
| `-o / --output` | `<书名>.pdf`（当前目录） | 输出路径；父目录会自动创建 |
| `--mode` | `vector` | `vector` = 原版矢量 PDF（需 Token）；`slices` = 免登录切片合成 |
| `--token` | 环境变量 `UC_TOKEN` | Base64(JSON) 形式的 UC_TOKEN，命令行传参会留在 shell 历史里，优先用环境变量 |
| `--token-from-shell` | 关 | 前两者都没有时，自动从 bash/zsh/fish/PowerShell 配置里读（见下面「取 Token 的几种方式」） |

未指定 `-o` 时文件名取自远端真实书名，并过滤 `\ / : * ? " < > |` 等非法字符、压缩空白为下划线。

### `audio` — 下载配套听力

```bash
python3 scripts/download_smartedu.py audio <contentId> [-o 输出目录] [--token TOKEN] [--token-from-shell]
```

- 默认输出目录 `<书名>_audios/`。
- 文件名格式 `NN_曲目标题.{mp3|ogg}`，序号按 `relation_audios.json` 原始顺序。
- 检索结果每条只保留**一个**音频文件：同一曲目在 JSON 里有 3~5 个等价变体（含 `clip-` 片头版），脚本按条目去重。
- 格式优先级：**有 Token** → 非 clip 的 mp3 → 非 clip 的 ogg → 其余；**无 Token** → 非 clip 的 ogg → 非 clip 的 mp3 → 其余。任一格式失败会继续尝试下一个变体。
- 结束时打印成功数；部分失败只警告（可用 Token 重试），**全部失败**才以非 0 退出。

### `doctor` — 环境自检

```bash
python3 scripts/download_smartedu.py doctor           # 离线检查
python3 scripts/download_smartedu.py doctor --verify  # 额外联网验证
```

分四段检查，每段给出 `✓ / ! / ✗ / -` 结论，最后汇总「结论」：

| 段 | 检查内容 |
|---|---|
| `[1] Token 来源` | 环境变量与各 shell 配置里找到的 Token（**只打印掩码**，如 `eyJz…fQ==`），并说明生效顺序 |
| `[2] Token 有效性` | Base64/JSON 是否可解析、`access_token`+`mac_key` 是否齐全、`expires_at` 是否过期（不足 24 小时给警告） |
| `[3] 缓存目录` | 实际可写位置；首选目录不可写时提示已回落及 `SMARTEDU_CACHE_DIR` |
| `[4] 网络与接口` | `--verify` 时拉一次全库索引，并用真 Token 打一次签名请求（走到鉴权失败为止即证明链路） |

退出码始终为 0（它是诊断命令，不是断言）；要看具体哪一步失败就看 `✗` 行。示例：

```text
[2] Token 有效性
  ✓ 字段：access_token + mac_key 齐全
  ✓ 有效期至 2026-10-24 15:11 UTC+08:00（剩余 6 天 19 小时）

[4] 网络与接口
  ✓ 公开接口：索引 3565 条，其中 654 条无分类（平台未收录，属正常）
  ✓ 签名链路：签名校验通过（详情接口正常返回：…英语 五年级上册）
```

---

## 工作原理

### 数据流

```text
data_version.json (module_version, urls)
        │
        ├── part_100.json ─┐
        ├── part_101.json  ├─► 合并去重（按 id）─► 本地 catalog.json
        ├── part_102.json  │      └─ tag_paths + tag_list → 面包屑
        └── part_103.json ─┘

details/{contentId}.json
        ├─ ti_items[ti_format=pdf] → ti_storages[0..2] (r1/r2/r3-private)  ─► 通道 A
        ├─ custom_properties.preview → .../transcode/image/{n}.jpg         ─► 通道 B
        └─ relation_audios.json      → 每曲 mp3/ogg 多个 ti_items          ─► 音频
```

| 接口 | 端点 | 鉴权 |
|---|---|---|
| 分类标签树 | `s-file-1…/zxx/ndrs/tags/tch_material_tag.json` | 公开 |
| 全库分片版本 | `s-file-1…/zxx/ndrs/resources/tch_material/version/data_version.json` | 公开 |
| 单书详情 | `s-file-2…/zxx/ndrv2/resources/tch_material/details/{id}.json` | 公开 |
| 配套听力 | `s-file-1…/zxx/ndrs/resources/{id}/relation_audios.json` | 公开 |
| 原版 PDF | `r{1,2,3}-ndr-private.ykt.cbern.com.cn/edu_product/esp/assets/…` | `X-ND-AUTH` 签名 |
| 页图切片 | `r{1,2,3}-ndr.ykt.cbern.com.cn/…/transcode/image/{n}.jpg` | **公开** |

### 面包屑解析

平台返回的 `tag_paths` 是一串以 `/` 分隔的 tag id，`tag_list` 则给出 `tag_id → tag_name` 映射。脚本：

1. 跳过全库共用的根节点 id（`f5261ccc-…`）；
2. 按 `tag_paths` 顺序查 `tag_list` 取名，缺失的层级直接略过；
3. 用 ` > ` 拼接。

**不要**拿 `tag_paths` 去反查标签树（`tch_material_tag.json`）：标签树的册次维度里没有「下册」节点，反查会让大量教材丢掉层级（历史上 1,920 条受影响）。

---

## 两条 PDF 通道对比

| | 通道 A：私有对象存储 | 通道 B：公网切片 |
|---|---|---|
| 入口 | `details.ti_items[ti_format=pdf].ti_storages` | `custom_properties.preview.*` 的同类目录 |
| 主机 | `r1/r2/r3-ndr-private.ykt.cbern.com.cn` | `r1/r2/r3-ndr.ykt.cbern.com.cn` |
| 内容 | 出版方原版 PDF | `transcode/image/{n}.jpg`，1300×1842 |
| 鉴权 | `X-ND-AUTH` MAC 签名 | 无 |
| 体积（实测） | 44.4 MB | 61.0 MB |
| 缩放 | 矢量，任意放大清晰 | 位图，放大有损 |
| 文本层 | 多为矢量描边，**不可复制/搜索** | 无 |
| 适用 | 存档、打印、缩放阅读 | 无账号兜底 |

> 通道 A 的 PDF 由出版方用方正 PDF 库生成（`Producer: Founder PDF Library`），内嵌 3 种 CID CFF 字体，含 ToUnicode 映射。实测该教材 110 页全文 `pdftotext` 只能抽出约 1.1 KB 文本，集中在第 52、59 页填空题；其余文字是矢量描边，无法复制或搜索。**可以指望它做“有文字层”的兜底，但不要指望全文可检索**，整书检索仍需 OCR。

---

## 签名算法详解

`UC_TOKEN` 来源：登录后的浏览器 Cookie `UC_TOKEN-xxx`，或 localStorage 中 `ND_UC_AUTH-xxx` 的 `&token=` 值。形式为 `Base64(URL-safe JSON)`，解码后含：

```json
{"access_token": "...", "mac_key": "...", "expires_at": ...}
```

有效期实测为 **签发后整 7 天**（`expires_at` 是带时区的 ISO 时间串，如 `2026-10-24T15:11:39.299+0800`，**不是** Unix 时间戳）。解码自查：

```bash
python3 - <<'PY'
import os, base64, json
t = os.environ["UC_TOKEN"].replace("-", "+").replace("_", "/")
t += "=" * (-len(t) % 4)
print(json.loads(base64.b64decode(t))["expires_at"])
PY
```

过期后签名请求返回 401/403，需重新从浏览器复制。

签名原文（脚本内 `sign_nd_mac`）：

```python
nonce    = f"{毫秒时间戳}:{8位随机大写字母数字}"
sign_str = f"{nonce}\nGET\n{原始未编码 path}\n{host}\n"
mac      = base64(hmac_sha256(mac_key, sign_str))
```

请求头：

```text
X-ND-AUTH: MAC id="{access_token}",nonce="{nonce}",mac="{mac}"
```

两个容易踩的点，脚本都已处理：

1. **签名用未编码的原始 URL，请求用转义后的 URL**。CDN 路径里带中文书名，先 `quote` 再签名必然 401。
2. **只签 path，不含 query、不含协议头**，`host` 也不带端口。

Token 解析对 `Bearer ` 前缀、URL-safe 变体（`-`/`_`）和缺失 padding 都做了兼容。

---

## 切片合成详解

`--mode slices` 的完整流程：

1. 从 `custom_properties.preview` 里取编号最小的 `Slide{ n }` 作为基准（字典顺序不可靠），统一改写主机为 `r1-`，去掉 query/fragment，得到 `…/transcode/image` 目录。
2. 从 `1.jpg` 顺序递增抓取；每页在 `r1/r2/r3` 三镜像间依次重试。
3. 页码越界时平台返回 403；允许 1~2 页编号断档，**连续 3 次失败**才判定到底（上限 `MAX_SLICES = 400`）。
4. 已下载的页缓存在缓存目录 `slices_{contentId}/`，中断后重跑只补缺页。
5. 用内置 **SOF 段解析器** 从 JPEG 字节流读宽高（不依赖 Pillow），按 `PDF_DPI = 150` 换算页面尺寸：1300×1842 px → 624×884 pt，接近 A4。
6. 把 JPEG 原始字节以 `/Filter /DCTDecode` 直接写进 PDF，**不重编码，字节级无损**。

产出的 PDF 是手写的 `%PDF-1.4` 结构：Catalog → Pages → 每页 `Page + Image XObject + Content Stream`，末尾补齐 xref 表与 trailer。先写 `*.part` 再原子替换，失败不会留下半截文件。

---

## 缓存机制

| 缓存文件 | 内容 | 失效条件 |
|---|---|---|
| `smartedu_catalog.json` | 全库索引（id / title / breadcrumbs / kind） | 远端 `module_version` 变化 |
| `smartedu_catalog.version` | 上次索引对应的版本号 | 同目录清理 |
| `smartedu_tags.json` | `tag_id → tag_name` 平面表 | 仅 `--refresh` 时重拉 |
| `slices_{contentId}/` | 切片页图（可按页续传） | 手动删除 |

**位置优先级**（`_cache_dir_usable()`）：

1. `SMARTEDU_CACHE_DIR`
2. `$XDG_CACHE_HOME/smartedu-textbook`，否则 `~/.cache/smartedu-textbook`
3. 回落 1：`<系统临时目录>/smartedu-textbook-{uid}`
4. 回落 2：`~/.smartedu-textbook`

回落目录**按用户 uid 隔离**，并校验目录属主就是当前用户、`chmod 700`、写探针验证——避免共享 `/tmp` 被其他本机用户预置伪造的索引 JSON。

> 某些沙箱环境（例如只允许写工作区的 agent 环境）会让 `~/.cache` 不可写，脚本会静默落到第 3 级。想固定位置就显式设 `SMARTEDU_CACHE_DIR=/path/to/cache`。

写索引和标签缓存都用「临时文件 + `os.replace`」，进程被 kill 也不会留下损坏的 JSON；读到损坏 JSON 会当作未命中重新拉取。

---

## 实测数据

以 `27eb5941-f566-431a-a2a6-92e45a2995e1`（教科版英语五年级上册）为例：

| 操作 | 结果 |
|---|---|
| `search 小学 英语 教科版 五年级` | 2 条命中，面包屑完整到「上册/下册」 |
| `download`（带 UC_TOKEN） | 44.4 MB，MD5 与平台一致，秒级完成 |
| `download --mode slices` | 110 页，61.0 MB，冷缓存约 13 秒 |
| `audio`（带 UC_TOKEN） | 32 曲全部落 **mp3**（192 kbps / 44.1 kHz / 立体声，ID3v2.4），共 67 MB |
| `audio`（免登录） | 32 曲全部落 ogg；mp3 直链返回 401 后自动回退 |
| 切片尺寸 | 1300×1842 px → 624×884 pt（150 dpi） |

Token 有效期实测为 **7 天**（`expires_at` 比签发时间晚整 7 天，带时区偏移，如 `2026-10-24T15:11:39.299+0800`）。

全库索引分布（`module_version = 1254188463`）：

| `resource_type_code` | 条数 | 说明 |
|---|---|---|
| `assets_document` | 3,242 | 教材 + 教学指南/课件 |
| `thematic_course` | 323 | 专题课程 |
| **合计** | **3,565** | 其中 2,911 条带「电子教材」分类，654 条**分类为空** |

---

## 排错手册

| 现象 | 原因 | 处理 |
|---|---|---|
| 下载 PDF `HTTP 401` | 签名原文的 path 被 URL-encode，或 Token 过期/损坏 | 脚本已强制 `unquote` 后签名；仍 401 就重新取 Token，或改用 `--mode slices` |
| 下载 PDF `HTTP 403` | 缺 `X-ND-AUTH`、Token 过期（签发后 7 天） | 重新提取 Token；或走切片通道 |
| 音频 mp3 `401` | 平台对 mp3 直链收紧权限，**非脚本问题** | 自动回退 ogg；需要 mp3 请提供有效 Token |
| 公开接口 `401/403` | 平台风控/限流，与该接口的 Token 无关 | 稍后重试；持续失败可能接口已变更 |
| 切片模式报「未检测到公开切片预览地址」 | 该资源没有 `custom_properties.preview`（多见于非教材资源） | 只能走 vector 通道 |
| 切片页数偏少 | 中间断档超过 2 页，或超过 `MAX_SLICES` | 检查 `slices_{contentId}/` 里缺哪页；必要时调大 `MAX_SLICES` |
| 进度卡住/进程被杀 | 下载走 `*.part` 临时文件 | 直接重跑，失败部分会自动清理/续传 |
| 搜不到 | AND 匹配、关键词过长 | 减词、用书名主体，或 `--refresh` |
| `UC_TOKEN 不是合法的 Base64(JSON)` | 复制时截断、带上了 `Bearer` 之外的字符 | 重新从浏览器复制完整值 |
| 缓存写不进去 | `~/.cache` 不可写（沙箱/只读挂载） | 设 `SMARTEDU_CACHE_DIR` 到可写目录 |
| 不知道 Token 到底有没有、在不在有效期 | — | 跑 `python3 scripts/download_smartedu.py doctor --verify`，四段体检一次看全 |

所有错误都以 `错误：……` 开头打印到 **stderr** 并返回退出码 1；`Ctrl-C` 返回 130。

### 取 Token 的几种方式（跨 shell / 跨平台）

Token 是约 1.1 KB 的 Base64 长串。下面三种方式按「推荐程度」排列，任选其一；都只用 `python3`（本工具的依赖），不依赖 zsh 或 GNU grep。

**方式 A：直接命令行传参**（最省事，临时用一次）

```bash
# 各 shell 通用写法，注意两端引号不能省
python3 scripts/download_smartedu.py download <contentId> --token "<粘贴 Token>"
```

缺点：会留在 shell 历史里。彻底用完建议 `history -d` 删掉，或改用下面两种。

**方式 B：环境变量**（变量设置一次，多次下载复用）

| Shell | 写法 | 备注 |
|---|---|---|
| bash / zsh / ksh | `export UC_TOKEN='<Token>'` | 写进 `~/.bashrc` / `~/.zshrc` 可持久化 |
| fish | `set -gx UC_TOKEN '<Token>'` | fish 语法，`export` 不可用 |
| PowerShell | `$env:UC_TOKEN = '<Token>'` | 会话级；持久化用 `[Environment]::SetEnvironmentVariable` |
| cmd.exe | `set UC_TOKEN=<Token>` | 会话级；持久化用 `setx` |

Token 里有 `+ / =` 等字符，**PowerShell 和 cmd 下必须用单引号包住**，否则部分字符会被解释。

**方式 C：让脚本自己去读配置**（不用复制粘贴，也不用手搓脚本）

已经写过一次 Token、但不想再复制的话，加 `--token-from-shell`，脚本会按 **shell 语法**（而不是按平台猜测）扫描常见配置：

| 扫描位置 | 语法 |
|---|---|
| `~/.bashrc`、`~/.bash_profile` | `export UC_TOKEN=...` |
| `~/.zshrc`、`~/.zprofile`、`~/.zshenv` | `export UC_TOKEN=...` |
| `$XDG_CONFIG_HOME/fish/config.fish`（默认 `~/.config/fish/`） | `set -gx UC_TOKEN ...` |
| PowerShell profile（`$XDG_CONFIG_HOME/powershell/`、`~/Documents/PowerShell/`、`~/OneDrive/Documents/PowerShell/`） | `$env:UC_TOKEN = ...` |

```bash
python3 scripts/download_smartedu.py download <contentId> --token-from-shell
python3 scripts/download_smartedu.py audio <contentId> --token-from-shell
```

细节：

- 取**文件中最后出现的**一处定义；引号、行尾注释、`--token` 前空格都能正确识别。
- 顺序固定为 `--token` > 环境变量 `UC_TOKEN` > shell 配置，不会意外覆盖你临时传给命令行的值。
- 配置文件在别处（改过 `ZDOTDIR`、PowerShell 用了 OneDrive 文档目录）时，脚本也会扫 `XDG_CONFIG_HOME` 与 OneDrive 路径；仍找不到就报错并列出查过的文件。
- 先跑 `doctor` 可以看到它到底从哪个文件读到了什么（只显示掩码）。

> 注意：Token 有效期实测为签发后 **7 天**。写进 `~/.bashrc` / `~/.zshrc` / `config.fish` 这类配置文件里方便，但**不要把文件内容贴到 issue、聊天记录或日志里**——那等于把账号凭证公开。更稳妥的做法是放在一个权限 600 的独立文件（如 `~/.config/smartedu-token`）里，用 `UC_TOKEN="$(cat …)"` 读。

---

## 已知限制

- **文本不可搜索**：两条通道都拿不到可靠文本层，需要检索请做 OCR。
- **654 条资源无分类**：`assets_document` 里平台本身未收录分类面包屑，检索输出会显示「平台未收录分类」提示。
- **Token 会过期**：签名通道没有自动续期，也没有登录流程，需人工从浏览器取。
- **音视频只有听力**：脚本目前只处理 `relation_audios`（mp3/ogg），不涉及课文视频、课件 PPT 等其它 `ti_items`。
- **切片是位图**：1300×1842 对 A4 阅读够用，但打印到 A3 以上会糊。

---

## 项目结构

```text
smartedu-textbook/
├── SKILL.md                      # 面向 AI Agent 的精简指令（Agent 加载入口）
├── README.md                     # 本文档
└── scripts/
    └── download_smartedu.py      # 单文件 CLI，约 730 行，仅标准库
```

脚本按职责分区：

| 区域 | 函数 |
|---|---|
| 错误与 HTTP | `SmartEduError`、`_describe_http_error`、`http_get_json` |
| 缓存 | `_cache_dir_usable`、`_atomic_write_json`、`load_catalog`、`load_tag_dict` |
| 签名 | `sign_nd_mac`、`parse_uc_token` |
| 下载原语 | `_download_to_file`、`_quote_url`、`_fetch_with_fallback` |
| 通道 A | `download_vector_pdf` |
| 通道 B | `_slice_base_dir`、`_page_mirrors`、`_jpeg_size`、`_write_jpeg_pdf`、`download_slices_pdf` |
| 音频 | `download_audios` |
| Token 获取 | `decode_uc_token`、`token_expires_at`、`scan_shell_configs`、`resolve_token` |
| 自检 | `cmd_doctor`、`_doctor_probe_public`、`_doctor_probe_signed`、`_mask` |
| CLI | `cmd_search`、`cmd_download`、`cmd_audio`、`build_parser`、`main` |

---

## 免责声明

本工具仅访问平台公开接口与公开 CDN 资源，用于个人学习与研究。教材版权归各出版社及教材编写机构所有，请遵守平台服务条款，勿将下载内容用于商业分发。
