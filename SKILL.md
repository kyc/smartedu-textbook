---
name: smartedu-textbook
description: >-
  Search, index, and download textbooks and accompanying audio materials from the National
  Smart Education Platform of China (国家中小学智慧教育平台, basic.smartedu.cn). Covers the full
  catalog (3,565 resources incl. PEP, Yuwen, Math, English textbooks), offline HMAC-SHA256
  signed vector PDF download, zero-token image-slice PDF export with byte-identical JPEG embedding,
  listening-audio extraction, and a cross-shell `doctor` self-check for the UC_TOKEN credential.
  Use when the user wants to find, download, or batch-fetch Chinese school textbooks
  (电子教材/教材) or their 听力音频.
---

# SmartEdu 教材与听力资源检索下载技能 (smartedu-textbook)

从**国家中小学智慧教育平台 (basic.smartedu.cn)** 检索全量教材索引，并下载原版矢量 PDF、免登录切片 PDF 或配套听力音频。

脚本：`python3 <skill-dir>/scripts/download_smartedu.py`（仅依赖 Python 3 标准库）

> 完整原理、参数说明、实测数据与排错手册见同目录 [`README.md`](README.md)；本文只保留 Agent 执行所需的要点。

---

## 一、 快速命令速查

| 操作目标 | 命令 | 说明 |
|---|---|---|
| **全库关键词检索** | `... search 小学 英语 教科版 五年级` | 多词 AND 匹配书名+分类面包屑，输出 `contentId`。 |
| **结构化检索输出** | `... search 人教版 数学 --json --limit 50` | 输出 JSON，便于程序二次处理。 |
| **刷新本地索引** | `... search 语文 --refresh` | 重新拉取分片清单（按 `module_version` 判断是否需要重拉）。 |
| **原版矢量 PDF**（推荐） | `... download <contentId> -o out.pdf` | 需 `UC_TOKEN`。离线计算 HMAC-SHA256 签名，直连私有 CDN。 |
| **免登录切片 PDF** | `... download <contentId> --mode slices -o out.pdf` | 零 Token，公开通道拉原图并字节级无损封装为 PDF。 |
| **配套听力音频** | `... audio <contentId> -o ./audios` | 每曲只落一份，ogg/mp3 自动回退。 |
| **环境自检** | `... doctor --verify` | 查 Token 来源/有效期、缓存目录、接口与签名。 |

**缓存位置**：默认 `$XDG_CACHE_HOME/smartedu-textbook`（即 `~/.cache/smartedu-textbook`），可用环境变量 `SMARTEDU_CACHE_DIR` 覆盖。skill 目录本身**不需要写权限**。

---

## 二、 平台数据源

```text
data_version.json (module_version)
   ├── part_100.json ─┐
   ├── part_101.json  ├── 全库索引，每条自带 tag_list(tag_id→tag_name) 与 tag_paths
   ├── part_102.json  │    ⇒ 直接本地解析面包屑，无需再查标签树
   └── part_103.json ─┘
details/{contentId}.json        → ti_items：pdf / jpg / folder，含 ti_storages（r1/r2/r3 三镜像）
custom_properties.preview       → 公开切片图 .../transcode/image/{page}.jpg（无防盗链）
relation_audios.json            → 配套听力，每曲含 mp3 / ogg / clip- 变体多个 ti_items
```

| 用途 | 端点 |
|---|---|
| 分类标签树（188 个节点） | `https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/tags/tch_material_tag.json` |
| 全库分片版本 | `https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/resources/tch_material/version/data_version.json` |
| 单书详情 | `https://s-file-2.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{contentId}.json` |
| 配套听力 | `https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/resources/{contentId}/relation_audios.json` |

### 两条下载通道

| | 通道 A：私有对象存储 | 通道 B：公网切片 |
|---|---|---|
| 主机 | `r1/r2/r3-ndr-private.ykt.cbern.com.cn` | `r1/r2/r3-ndr.ykt.cbern.com.cn` |
| 内容 | 出版方原版 PDF（`ti_format == "pdf"`） | `transcode/image/{n}.jpg` 高清页图 |
| 鉴权 | 需 `X-ND-AUTH` MAC 签名头 | 公开，无签名 |
| 质量 | 出版方排版，文字多为矢量描边，缩放清晰 | 1300×1842 位图，够看但放大有损 |
| 体积 | 更小（实测 44.4 MB） | 更大（实测 61 MB） |
| 适用 | 长期存档、打印、缩放阅读 | 无账号兜底 |

> **关于文本层**：通道 A 的 PDF 由出版方生成。实测该教材 110 页中仅 2 页（52、59 页，填空题）含真实文本层，其余页面的文字是**矢量描边**——视觉上可任意缩放，但无法复制或搜索。需要文字检索请另行 OCR，不要指望 `pdftotext` 能出结果。

---

## 三、 标准工作流

### 工作流 1：检索 `contentId`

```bash
python3 <skill-dir>/scripts/download_smartedu.py search 小学 英语 教科版 五年级
```

输出示例（面包屑含到「下册」）：

```text
检索到 2 条符合条件的资源:

序号   Content ID                             教材名称与分类
------------------------------------------------------------------------------------------
1    27eb5941-f566-431a-a2a6-92e45a2995e1   （根据2022年版课程标准修订）义务教育教科书·英语 五年级上册
     └─ 分类: 电子教材 > 小学 > 英语 > 教科版（主编：龚亚夫） > 五年级 > 上册
2    285ae1ca-33f3-40b5-b9c8-c6fd8256168b   义务教育教科书·英语（三年级起点）五年级下册
     └─ 分类: 电子教材 > 小学 > 英语 > 教科版（主编：龚亚夫） > 五年级 > 下册
```

> 全库 3,565 条中有 654 条是 `assets_document`（信息科技教学指南、课件等），平台本身未收录分类，面包屑为空属正常现象，不代表索引出错。

### 工作流 2：下载原版矢量 PDF

需要 `UC_TOKEN`——从已登录浏览器的 Cookie `UC_TOKEN-xxx` 或 localStorage `ND_UC_AUTH-xxx` 的 `&token=` 值复制，形式为 `Base64(JSON)`，解码后含 `access_token` 与 `mac_key`（以及 `expires_at`，实测签发后 7 天有效）。

**先自检**（诊断命令，只打印掩码，不会泄露凭证）：

```bash
python3 <skill-dir>/scripts/download_smartedu.py doctor --verify
```

它会报告 Token 从哪来、是否过期、缓存目录是否可写、公开接口与签名链路是否通。

```bash
# 四种给 Token 的方式，优先级：--token > 环境变量 > shell 配置
python3 <skill-dir>/scripts/download_smartedu.py download "<contentId>" --token "<值>"
export UC_TOKEN="<值>"          # fish: set -gx；PowerShell: $env:UC_TOKEN
python3 <skill-dir>/scripts/download_smartedu.py download "<contentId>" --token-from-shell
python3 <skill-dir>/scripts/download_smartedu.py download "<contentId>" --mode slices   # 零 Token 兜底

# 示例
python3 <skill-dir>/scripts/download_smartedu.py download "27eb5941-f566-431a-a2a6-92e45a2995e1" -o english_5a.pdf
```

`--token-from-shell` 按 shell 语法扫描 `~/.bashrc`、`~/.zshrc`、`~/.zprofile`、`~/.zshenv`、fish 的 `config.fish` 与 PowerShell profile（含 `XDG_CONFIG_HOME` 与 OneDrive 路径），不依赖 zsh 或 GNU grep。

签名算法（脚本已内置 `sign_nd_mac`）：

```python
nonce = f"{int(time.time() * 1000)}:{8位随机大写字母数字}"
sign_str = f"{nonce}\nGET\n{unquoted_path}\n{host}\n"   # path 必须是未编码的原始中文
mac = base64.b64encode(hmac.new(mac_key.encode(), sign_str.encode(), hashlib.sha256).digest()).decode()
X-ND-AUTH: MAC id="{access_token}",nonce="{nonce}",mac="{mac}"
```

要点：**签名用原始 URL，请求用转义后的 URL**。脚本两者都已处理。

### 工作流 3：免登录切片合成

```bash
python3 <skill-dir>/scripts/download_smartedu.py download "27eb5941-f566-431a-a2a6-92e45a2995e1" --mode slices -o english_5a_slices.pdf
```

- 实测该教材 110 页，冷缓存约 13 秒完成，产出约 61 MB。
- 切片按 `…/image/{n}.jpg` 顺序抓取；**页码不存在时平台返回 403**，脚本允许 1–2 页断档，连续 3 页失败才判定到底。
- 每页分别在 r1/r2/r3 三镜像间自动重试。
- 封装方式：把原始 JPEG 字节以 `DCTDecode` 直接写入 PDF，**字节级无损**（不重编码）；页面按 150dpi 换算，约 624×884 pt，接近 A4。
- **不需要 Pillow**：JPEG 尺寸由脚本内置的 SOF 解析器读取。

### 工作流 4：下载配套听力音频

```bash
python3 <skill-dir>/scripts/download_smartedu.py audio "27eb5941-f566-431a-a2a6-92e45a2995e1" -o ./english_5a_audios
```

- `relation_audios.json` 中每个曲目会列出多个等价变体（原版 mp3、`clip-` 片头版、ogg、带长文件名的 mp3），脚本**按条目去重，每曲只存一份**。
- 免登录实测：ogg 可全部下载，mp3 直链常返回 401；脚本自动按「非 clip 的 ogg → 非 clip 的 mp3 → 其余」回退。

---

## 四、 常见问题与排错

| 现象 | 原因与处理 |
|---|---|
| **HTTP 401**（下载 PDF 时） | 签名原文的 path 被 URL-encode，或 `UC_TOKEN` 过期/损坏。脚本已强制 `unquote` 后签名；若仍 401，重新获取 Token。 |
| **HTTP 403**（下载 PDF 时） | 缺 `X-ND-AUTH` 或 Token 过期（通常 7–14 天）。重新提取 Token。 |
| **音频 mp3 401** | 平台对 mp3 直链收紧权限，**不是脚本问题**。脚本会自动回退 ogg；如需 mp3 请提供有效 Token。 |
| **公开接口 401/403** | 如检索或取详情时报鉴权类错误，多半是平台风控/限流，与 Token 无关；稍后重试。 |
| **进度卡住/中断** | 下载写入 `*.part` 临时文件，失败自动清理；重跑即可。 |
| **搜索不到** | 关键词是 AND 匹配；减少词、用教材上的准确书名，或加 `--refresh`。 |

> 旧版本曾出现：`tag_paths` 反查标签树导致 **1,920 条教材丢「下册」层级**（标签树的册次维度里没有「下册」节点）。现改为直接读分片自带的 `tag_list`，已修复。
