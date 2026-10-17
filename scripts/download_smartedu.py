#!/usr/bin/env python3
"""
SmartEdu (国家中小学智慧教育平台) 全平台教材库（3,565 册）索引检索与纯代码极速下载工具

功能特性：
  1. 完整目录索引：自动拉取并本地缓存全网 3,565 册教材索引与分类标签树，毫秒级多关键词检索。
  2. 智能分类呈现：基于书目自带 tag_list 还原完整面包屑（电子教材 > 小学 > 英语 > 教科版 > 五年级 > 上册）。
  3. 离线签名极速下载 (vector 模式)：本地离线计算 NetDragon HMAC-SHA256 签名，满速下载原版矢量 PDF。
  4. 免登录高清切片下载 (slices 模式)：零 Token 依赖，拉取公网开放的高清原图并按 72dpi 封装为 PDF。
  5. 配套听力音频下载 (audio 模式)：从 relation_audios.json 拉取 MP3/OGG，同样支持签名与免登录双通道。

用法速览：
  download_smartedu.py search 小学 英语 教科版 五年级 [--refresh] [--json]
  download_smartedu.py download <contentId> [-o out.pdf] [--mode vector|slices] [--token ...]
  download_smartedu.py audio    <contentId> [-o dir/] [--token ...]
"""

import sys
import os
import re
import time
import json
import base64
import random
import hmac
import hashlib
import argparse
import tempfile
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Dict, Optional, Tuple

VERSION_API = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/resources/tch_material/version/data_version.json"
TAG_TREE_API = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/tags/tch_material_tag.json"
DETAILS_API = "https://s-file-2.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{content_id}.json"
AUDIO_RELATION_API = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/resources/{content_id}/relation_audios.json"

# tag_paths 的首段是全库共用根节点 id，无对应名称，解析时跳过
ROOT_TAG_ID = "f5261ccc-b7a5-42f4-b015-610dcec58c60"

USER_AGENT = "Mozilla/5.0 (SmartEdu CLI)"
MAX_SLICES = 400
PDF_DPI = 150  # 平台切片为 1300x1842px，按 150dpi 换算约 624x884pt，接近 A4 且不拉伸失真

# 缓存目录：优先环境变量，其次 XDG_CACHE_HOME / ~/.cache，最后回落到系统临时目录
CACHE_ENV = os.environ.get("SMARTEDU_CACHE_DIR", "")
if CACHE_ENV:
    CACHE_DIR = Path(CACHE_ENV).expanduser()
else:
    _xdg = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    CACHE_DIR = _xdg / "smartedu-textbook"

CATALOG_CACHE_FILE = CACHE_DIR / "smartedu_catalog.json"
TAG_CACHE_FILE = CACHE_DIR / "smartedu_tags.json"


class SmartEduError(RuntimeError):
    """面向用户的、带排错提示的错误。"""


def _cache_dir_usable() -> Path:
    """返回一个可写的缓存目录。

    首选 SMARTEDU_CACHE_DIR / XDG 缓存目录；不可写时回落到**按用户隔离**的目录，
    避免共享 /tmp 被其他本机用户预置伪造的索引 JSON。
    """
    try:
        user_tag = str(os.getuid())
    except AttributeError:  # Windows
        user_tag = re.sub(r"[^A-Za-z0-9_.-]", "_", os.environ.get("USERNAME", "user"))

    fallbacks = [
        Path(tempfile.gettempdir()) / f"smartedu-textbook-{user_tag}",
        Path.home() / ".smartedu-textbook",
    ]
    for candidate in (CACHE_DIR, *fallbacks):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            # 目录必须属于当前用户，防止使用他人预置的目录
            if hasattr(os, "getuid") and candidate.stat().st_uid != os.getuid():
                continue
            os.chmod(candidate, 0o700)
            probe = candidate / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    raise SmartEduError("无法创建缓存目录。请设置环境变量 SMARTEDU_CACHE_DIR 指向一个可写路径。")


def _short(url: str, limit: int = 120) -> str:
    """截断过长的 URL，避免错误信息刷屏。"""
    return url if len(url) <= limit else url[: limit - 3] + "..."


def _describe_http_error(err: urllib.error.HTTPError, target: str, needs_auth: bool = False) -> str:
    """needs_auth=False 表示公开接口，此时不应把错误归因到 UC_TOKEN。"""
    if not needs_auth:
        if err.code == 404:
            return f"HTTP 404 资源不存在（{_short(target)}）。请确认 contentId 是否正确。"
        if err.code in (401, 403):
            return (
                f"HTTP {err.code} 公开接口拒绝访问（{_short(target)}）。\n"
                "  这通常是平台风控/限流，而非 Token 问题（该接口本就不需要登录）。\n"
                "  处理：稍后重试；如持续失败，可能是平台接口已变更。"
            )
        return f"HTTP {err.code} 请求失败（{_short(target)}）：{err.reason}"

    if err.code == 401:
        return (
            f"HTTP 401 签名校验失败（{_short(target)}）。\n"
            "  常见原因：① 签名原文的 path 被 URL-encode（必须使用未编码的原始中文路径）；\n"
            "           ② UC_TOKEN 已过期或格式损坏。\n"
            "  处理：重新从浏览器复制最新 UC_TOKEN，或改用 --mode slices 免登录下载。"
        )
    if err.code == 403:
        return (
            f"HTTP 403 拒绝访问（{_short(target)}）。\n"
            "  常见原因：UC_TOKEN 过期（通常有效期 7-14 天），或该资源需登录后访问。\n"
            "  处理：重新提取 UC_TOKEN；或改用 --mode slices 免登录下载。"
        )
    if err.code == 404:
        return f"HTTP 404 资源不存在（{_short(target)}）。请确认 contentId 是否正确。"
    return f"HTTP {err.code} 请求失败（{_short(target)}）：{err.reason}"


def http_get_json(url: str, timeout: int = 20, auth_header: Optional[str] = None):
    headers = {"User-Agent": USER_AGENT}
    if auth_header:
        headers["X-ND-AUTH"] = auth_header
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SmartEduError(_describe_http_error(e, url, needs_auth=auth_header is not None)) from None
    except urllib.error.URLError as e:
        raise SmartEduError(f"网络连接失败（{url}）：{e.reason}") from None
    except json.JSONDecodeError:
        raise SmartEduError(f"接口返回的不是合法 JSON（{url}），平台接口可能已变更。") from None


def _atomic_write_json(path: Path, data) -> None:
    """先写临时文件再替换，避免中断后留下半截缓存。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_tag_dict(force_refresh: bool = False) -> Dict[str, str]:
    cache_dir = _cache_dir_usable()
    tag_file = cache_dir / TAG_CACHE_FILE.name
    if not force_refresh and tag_file.exists():
        try:
            return json.loads(tag_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    print("正在同步分类标签树 (tch_material_tag.json)...", file=sys.stderr)
    raw_data = http_get_json(TAG_TREE_API)
    tag_map: Dict[str, str] = {}

    def walk(node):
        if isinstance(node, dict):
            tag_id = node.get("tag_id")
            name = node.get("tag_name")
            if tag_id and name:
                tag_map[tag_id] = name.strip()
            for key in ("children", "hierarchies"):
                if node.get(key) is not None:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(raw_data)
    _atomic_write_json(tag_file, tag_map)
    return tag_map


def load_catalog(force_refresh: bool = False) -> List[dict]:
    cache_dir = _cache_dir_usable()
    catalog_file = cache_dir / CATALOG_CACHE_FILE.name
    version_file = cache_dir / "smartedu_catalog.version"

    # 先取远端版本号：版本未变直接复用缓存，refresh 也只在真正有新版本时重拉分片
    try:
        remote_version = int(http_get_json(VERSION_API).get("module_version") or 0)
    except SmartEduError as e:
        if catalog_file.exists():
            print(f"警告：无法校验远端版本（{e}），使用本地缓存。", file=sys.stderr)
            return json.loads(catalog_file.read_text(encoding="utf-8"))
        raise

    if not force_refresh and catalog_file.exists():
        try:
            cached = json.loads(catalog_file.read_text(encoding="utf-8"))
            cached_version = int(version_file.read_text().strip()) if version_file.exists() else 0
            if cached and cached_version == remote_version:
                return cached
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    print("正在获取教材库全局分片清单 (data_version.json)...", file=sys.stderr)
    ver_data = http_get_json(VERSION_API)
    part_urls = [u.strip() for u in (ver_data.get("urls") or "").split(",") if u.strip()]

    all_books: List[dict] = []
    seen_ids = set()
    for u in part_urls:
        print(f"正在拉取教材分片: {u.split('/')[-1]}...", file=sys.stderr)
        for item in http_get_json(u):
            book_id = item.get("id")
            if not book_id or book_id in seen_ids:
                continue
            seen_ids.add(book_id)
            title = item.get("title") or (item.get("global_title") or {}).get("zh-CN", "")

            # 分片自带 tag_list（tag_id -> tag_name），按 tag_paths 的层级顺序还原面包屑。
            # 不要反查标签树：其中间层/隐藏标签并不齐全，会导致全部书都缺分类。
            name_by_id = {
                t.get("tag_id"): t.get("tag_name")
                for t in (item.get("tag_list") or [])
                if t.get("tag_id") and t.get("tag_name")
            }
            breadcrumbs: List[str] = []
            tag_paths = item.get("tag_paths") or []
            if tag_paths and name_by_id:
                for tid in tag_paths[0].split("/"):
                    if tid == ROOT_TAG_ID:
                        continue
                    name = name_by_id.get(tid)
                    # 跳过 tagView 之类的伪维度（不参与 tag_paths），只按 path 顺序取
                    if name:
                        breadcrumbs.append(name)

            all_books.append({
                "id": book_id,
                "title": title,
                "breadcrumbs": " > ".join(breadcrumbs),
                "kind": item.get("resource_type_code", ""),
            })

    print(f"全平台索引完成，共载入 {len(all_books)} 条资源。", file=sys.stderr)
    _atomic_write_json(catalog_file, all_books)
    version_file.write_text(str(remote_version))
    return all_books


def get_book_details(content_id: str) -> dict:
    return http_get_json(DETAILS_API.format(content_id=content_id))


def get_relation_audios(content_id: str) -> List[dict]:
    return http_get_json(AUDIO_RELATION_API.format(content_id=content_id)) or []


def sign_nd_mac(url: str, access_token: str, mac_key: str, method: str = "GET") -> str:
    """网龙 UC-Auth 离线 HMAC-SHA256 签名算法。"""
    parsed = urllib.parse.urlparse(url)
    unquoted_path = urllib.parse.unquote(parsed.path)
    host = parsed.netloc

    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    rand_str = "".join(random.choice(chars) for _ in range(8))
    nonce = f"{int(time.time() * 1000)}:{rand_str}"

    sign_str = f"{nonce}\n{method.upper()}\n{unquoted_path}\n{host}\n"
    mac = base64.b64encode(
        hmac.new(mac_key.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    return f'MAC id="{access_token}",nonce="{nonce}",mac="{mac}"'


def parse_uc_token(uc_token: str) -> Tuple[str, str]:
    """解析 Base64(JSON) 形式的 UC_TOKEN，返回 (access_token, mac_key)。"""
    raw = uc_token.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    raw = raw.replace("-", "+").replace("_", "/")  # 兼容 urlsafe 变体
    raw += "=" * (-len(raw) % 4)  # 补齐 padding
    try:
        token_info = json.loads(base64.b64decode(raw))
    except Exception:
        raise SmartEduError(
            "UC_TOKEN 不是合法的 Base64(JSON)。\n"
            "  正确来源：浏览器 Cookie `UC_TOKEN-xxx` 或 localStorage `ND_UC_AUTH-xxx &token` 的值，"
            "解码后应含 access_token 与 mac_key 两个字段。"
        ) from None
    access_token, mac_key = token_info.get("access_token"), token_info.get("mac_key")
    if not access_token or not mac_key:
        raise SmartEduError("UC_TOKEN 缺少 access_token 或 mac_key 字段，可能复制不完整或已过期，请重新获取。")
    return access_token, mac_key


def _download_to_file(req: urllib.request.Request, output_path: Path, label: str) -> None:
    """带进度显示的流式下载；失败时清理半截文件，成功才落到目标路径。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_path.with_name(output_path.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(part_path, "wb") as out:
            total = int(resp.headers.get("content-length", 0) or 0)
            downloaded = 0
            while chunk := resp.read(1024 * 256):
                out.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    print(
                        f"\r{label} 进度: {downloaded * 100 // total}% "
                        f"({downloaded / 1048576:.1f}MB / {total / 1048576:.1f}MB)",
                        end="", file=sys.stderr, flush=True,
                    )
        print("", file=sys.stderr)
    except urllib.error.HTTPError as e:
        part_path.unlink(missing_ok=True)
        raise SmartEduError(_describe_http_error(e, req.full_url, needs_auth=True)) from None
    except urllib.error.URLError as e:
        part_path.unlink(missing_ok=True)
        raise SmartEduError(f"网络中断（{req.full_url}）：{e.reason}") from None
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise
    os.replace(part_path, output_path)


def _quote_url(url: str) -> str:
    """把 URL 中的空格/中文等字符转义（已转义的 %xx 保持不变）。

    音频直链文件名里常含空格（如 "Task B Which tips ..."），不转义会触发
    http.client.InvalidURL。
    """
    return urllib.parse.quote(url, safe=":/?&=#%")


def _fetch_with_fallback(urls: List[str], make_headers, timeout: int = 15) -> bytes:
    """依次尝试多个 CDN 镜像，返回首个成功的响应体。

    make_headers(raw_url) 必须每次都在循环内重新调用：签名头（X-ND-AUTH）的 nonce
    含时间戳且绑定具体 URL，复用上一次的 hdrs 会签错镜像。
    """
    last_err: Optional[Exception] = None
    for raw_url in urls:
        # 签名必须基于原始 URL，请求才用转义后的 URL
        url = _quote_url(raw_url)
        hdrs = make_headers(raw_url)
        authed = "X-ND-AUTH" in hdrs
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            # 已签名请求遇 401/403 = 鉴权失败，立即上抛（换镜像也是同一次鉴权）；
            # 公开请求的 403 通常只代表「该页不存在」，继续尝试下一个镜像
            if authed and e.code in (401, 403):
                raise SmartEduError(_describe_http_error(e, raw_url, needs_auth=True)) from None
            last_err = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
    raise SmartEduError(f"所有镜像均不可用：{last_err}")


def download_vector_pdf(details: dict, output_path: Path, uc_token: str) -> None:
    access_token, mac_key = parse_uc_token(uc_token)

    pdf_item = next((i for i in details.get("ti_items", []) if i.get("ti_format") == "pdf"), None)
    if not pdf_item:
        raise SmartEduError("该资源没有 PDF 文件项（可能本身不是教材）。可改用 --mode slices。")
    storages = pdf_item.get("ti_storages") or []
    if not storages:
        raise SmartEduError("PDF 文件项缺少 ti_storages 下载地址。")

    storage_url = storages[0]
    size_mb = pdf_item.get("ti_size", 0) / 1048576

    # 签名必须基于原始（未编码）URL；实际请求才使用转义后的 URL
    auth_header = sign_nd_mac(storage_url, access_token, mac_key)
    quoted_url = _quote_url(storage_url)

    print(f"正在从私有 CDN 下载原版矢量 PDF（约 {size_mb:.1f} MB）...", file=sys.stderr)
    req = urllib.request.Request(quoted_url, headers={"User-Agent": USER_AGENT, "X-ND-AUTH": auth_header})
    _download_to_file(req, output_path, "PDF")
    print(f"下载完成: {output_path} ({output_path.stat().st_size / 1048576:.1f} MB)")


def _slice_base_dir(details: dict) -> str:
    preview = (details.get("custom_properties") or {}).get("preview") or {}
    if not preview:
        raise SmartEduError("未检测到公开切片预览地址，该资源可能不支持切片模式。")

    # preview 形如 {"Slide6": url, ...}，字典顺序不可靠，取编号最小的 Slide 作为基准
    def slide_no(key) -> int:
        m = re.search(r"(\d+)", str(key))
        return int(m.group(1)) if m else 10 ** 9

    first_url = preview[sorted(preview, key=slide_no)[0]]
    # 统一到 r1 主机，保证基准目录稳定
    parsed = urllib.parse.urlparse(first_url)
    host = parsed.netloc
    for prefix in ("r2-", "r3-"):
        if host.startswith(prefix):
            host = "r1-" + host[len(prefix):]
            break
    return urllib.parse.urlunparse(parsed._replace(netloc=host, query="", fragment="")).rsplit("/", 1)[0]


def _page_mirrors(base_dir_url: str, page: int) -> List[str]:
    """同一路径在 r1/r2/r3 三个 CDN 上都有，依次尝试可提高稳定性。"""
    parsed = urllib.parse.urlparse(base_dir_url)
    host = parsed.netloc
    mirrors = [f"{base_dir_url}/{page}.jpg"]
    for prefix in ("r1-", "r2-", "r3-"):
        if host.startswith(prefix):
            for other in ("r1-", "r2-", "r3-"):
                if other != prefix:
                    mirrors.append(f"https://{other}{host[len(prefix):]}/{page}.jpg")
            break
    return mirrors


def _jpeg_size(data: bytes) -> Optional[Tuple[int, int]]:
    """从 JPEG 字节流中解析 SOF 段获取 (宽, 高)，无需 Pillow。"""
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    n = len(data)
    while i + 4 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        # SOF0..SOF15（排除 DHT=C4 / JPG=C8 / DAC=CC）包含宽高
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        i += 2 + seg_len
    return None


def _write_jpeg_pdf(pages: List[Path], output_path: Path, dpi: int) -> None:
    """把 JPEG 原始字节直接封装为 PDF（DCTDecode），不重编码，字节级无损。

    纯标准库实现，因此 slices 模式实际不再强制依赖 Pillow。
    """
    bodies: Dict[int, bytes] = {}
    n = len(pages)
    bodies[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{3 + i * 3} 0 R" for i in range(n)).encode()
    bodies[2] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (n, kids)

    for i, path in enumerate(pages):
        data = path.read_bytes()
        size = _jpeg_size(data)
        if not size:
            raise SmartEduError(f"无法解析图片尺寸（可能不是 JPEG）：{path}")
        w, h = size
        pw, ph = w * 72.0 / dpi, h * 72.0 / dpi
        page_n, img_n, cont_n = 3 + i * 3, 4 + i * 3, 5 + i * 3
        bodies[page_n] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
            b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
            % (pw, ph, img_n, cont_n)
        )
        bodies[img_n] = (
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (w, h, len(data))
            + data
            + b"\nendstream"
        )
        content = ("q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (pw, ph)).encode()
        bodies[cont_n] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"

    part_path = output_path.with_name(output_path.name + ".part")
    with open(part_path, "wb") as out:
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: Dict[int, int] = {}
        for num in sorted(bodies):
            offsets[num] = out.tell()
            out.write(b"%d 0 obj\n" % num + bodies[num] + b"\nendobj\n")
        xref_pos = out.tell()
        max_num = max(bodies)
        out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (max_num + 1))
        for i in range(1, max_num + 1):
            out.write(b"%010d 00000 n \n" % offsets.get(i, 0))
        out.write(
            b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (max_num + 1, xref_pos)
        )
    os.replace(part_path, output_path)


def download_slices_pdf(details: dict, output_path: Path) -> None:
    base_dir_url = _slice_base_dir(details)
    content_id = details.get("id") or "unknown"

    tmp_dir = _cache_dir_usable() / f"slices_{content_id}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    print("正在拉取公网高清转码切片（无需鉴权）...", file=sys.stderr)
    page_files: List[Path] = []
    consecutive_failures = 0
    for page in range(1, MAX_SLICES + 1):
        img_path = tmp_dir / f"{page:04d}.jpg"
        if not img_path.exists() or img_path.stat().st_size == 0:
            try:
                data = _fetch_with_fallback(
                    _page_mirrors(base_dir_url, page), lambda u: {"User-Agent": USER_AGENT}
                )
                img_path.write_bytes(data)
            except SmartEduError:
                # 允许 1-2 页编号断档；连续 3 页失败判定为到底
                consecutive_failures += 1
                print(f"\n第 {page} 页缺失，跳过（连续失败 {consecutive_failures}）", file=sys.stderr)
                if consecutive_failures >= 3:
                    break
                continue
        consecutive_failures = 0
        if not _jpeg_size(img_path.read_bytes()):
            img_path.unlink(missing_ok=True)
            continue
        page_files.append(img_path)
        print(f"\r已抓取页码: {page}", end="", file=sys.stderr, flush=True)

    print("", file=sys.stderr)
    if not page_files:
        raise SmartEduError("未获取到任何有效图像切片，切片模式不可用。")

    print(
        f"正在将 {len(page_files)} 页图像封装为 PDF（{PDF_DPI}dpi，原图字节级无损嵌入）...",
        file=sys.stderr,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jpeg_pdf(page_files, output_path, PDF_DPI)
    print(f"切片合成完成: {output_path}（{len(page_files)} 页，{output_path.stat().st_size / 1048576:.1f} MB）")


def download_audios(details: dict, out_dir: Path, uc_token: Optional[str]) -> int:
    """下载教材配套听力：每个曲目条目只落一份文件。

    单个条目在 relation_audios 中会列出多个等价变体（原版 mp3 / clip- 片头版 /
    ogg / 带长文件名的 mp3），因此按条目去重，只保存一份。
    平台对部分 mp3 直链收紧了权限（免登录恒 401），而 ogg 通常可公开下载，
    故按「非 clip 的 ogg → 非 clip 的 mp3 → 其余」顺序回退。
    """
    try:
        audio_list = get_relation_audios(details.get("id"))
    except SmartEduError as e:
        raise SmartEduError(f"未获取到配套音频清单：{e}") from None

    entries: List[Tuple[str, List[Tuple[str, List[str]]]]] = []
    for entry in audio_list:
        title = entry.get("title") or (entry.get("global_title") or {}).get("zh-CN", "")
        options: List[Tuple[str, List[str]]] = []
        for item in entry.get("ti_items") or []:
            fmt = (item.get("ti_format") or "").lower()
            storages = item.get("ti_storages") or []
            if fmt in ("mp3", "ogg") and storages:
                options.append((fmt, storages))
        if options:
            entries.append((title or entry.get("id", ""), options))

    if not entries:
        raise SmartEduError("该教材没有配套听力音频（relation_audios 为空或无 mp3/ogg 项）。")

    def is_clip(fmt_storages) -> bool:
        name = urllib.parse.unquote(fmt_storages[1][0].rsplit("/", 1)[-1])
        return name.startswith("clip-")

    if uc_token:
        access_token, mac_key = parse_uc_token(uc_token)
    else:
        print("未提供 UC_TOKEN，将尝试免登录直连音频（mp3 可能 401，会自动回退 ogg）。", file=sys.stderr)
        access_token, mac_key = None, None

    def headers(u):
        h = {"User-Agent": USER_AGENT}
        if mac_key:
            h["X-ND-AUTH"] = sign_nd_mac(u, access_token, mac_key)
        return h

    out_dir.mkdir(parents=True, exist_ok=True)
    total = len(entries)
    ok = 0
    failures: List[str] = []

    for idx, (title, options) in enumerate(entries, 1):
        # 优先级：非 clip 优先；有 Token 时 mp3 优先（原格式、兼容性好），
        # 无 Token 时 ogg 优先（公开通道可下，mp3 直链常 401），失败都会自动回退另一种格式
        prefer_lossless = mac_key is not None
        first_fmt = "mp3" if prefer_lossless else "ogg"
        options.sort(key=lambda o: (is_clip(o), o[0] != first_fmt))
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", title).strip()[:80] or f"audio_{idx}"
        saved = False
        for fmt, storages in options:
            dest = out_dir / f"{idx:02d}_{safe}.{fmt}"
            try:
                data = _fetch_with_fallback(storages, headers)
            except SmartEduError:
                continue
            dest.write_bytes(data)
            ok += 1
            saved = True
            print(f"[{idx}/{total}] {dest.name} ({len(data) / 1024:.0f} KB)", file=sys.stderr)
            break
        if not saved:
            failures.append(title[:50] or f"#{idx}")
            print(f"[{idx}/{total}] 失败（mp3/ogg 均不可用）：{(title or f'#{idx}')[:50]}", file=sys.stderr)

    print(f"音频下载完成：成功 {ok}/{total}，输出目录 {out_dir}")
    if failures:
        print(f"未能下载 {len(failures)} 条，多为平台对直链鉴权收紧所致；可用 UC_TOKEN 重试。", file=sys.stderr)
    if ok == 0:
        raise SmartEduError(
            "全部音频下载失败。免登录下平台常对音频直链返回 401，请设置有效 UC_TOKEN 后重试。"
        )
    return ok


def cmd_search(args) -> None:
    catalog = load_catalog(force_refresh=args.refresh)
    keywords = [w.lower() for k in args.keywords for w in k.split() if w]
    results = [
        item for item in catalog
        if all(kw in f"{item['title']} {item['breadcrumbs']}".lower() for kw in keywords)
    ]

    if args.json:
        print(json.dumps(results[: args.limit], ensure_ascii=False, indent=2))
        return

    print(f"\n检索到 {len(results)} 条符合条件的资源:\n")
    print(f"{'序号':<4} {'Content ID':<38} 教材名称与分类")
    print("-" * 90)
    for idx, item in enumerate(results[: args.limit], 1):
        print(f"{idx:<4} {item['id']:<38} {item['title']}")
        if item["breadcrumbs"]:
            print(f"     └─ 分类: {item['breadcrumbs']}")
        elif item.get("kind") == "assets_document":
            print("     └─ 分类: （平台未收录分类：教学指南/课件类资源）")
    if len(results) > args.limit:
        print(f"\n... 还有 {len(results) - args.limit} 条未展示，请增加关键词或调大 --limit。")
    if not results:
        print("建议：减少关键词、使用教材上的准确书名，或加 --refresh 刷新索引。")


def _safe_filename(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "", title).strip()
    return re.sub(r"\s+", "_", cleaned)[:120] or "textbook"


def cmd_download(args) -> None:
    details = get_book_details(args.id)
    real_title = details.get("title") or (details.get("global_title") or {}).get("zh-CN") or args.id
    out_path = Path(args.output) if args.output else Path(_safe_filename(real_title) + ".pdf")
    out_path = out_path.expanduser().resolve()

    if args.mode == "vector":
        token = args.token or os.environ.get("UC_TOKEN", "")
        if not token:
            raise SmartEduError(
                "vector 原版下载模式需要 UC_TOKEN。\n"
                '  方式一：export UC_TOKEN="<浏览器 Cookie UC_TOKEN-xxx 的值>"\n'
                "  方式二：加 --mode slices 使用免登录切片合成"
            )
        download_vector_pdf(details, out_path, token)
    else:
        download_slices_pdf(details, out_path)


def cmd_audio(args) -> None:
    details = get_book_details(args.id)
    token = args.token or os.environ.get("UC_TOKEN", "")
    out_dir = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path(_safe_filename(details.get("title") or args.id) + "_audios")
    )
    download_audios(details, out_dir, token or None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="国家智慧教育平台教材库检索与纯代码下载工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="根据关键词组合检索教材/资源")
    p_search.add_argument("keywords", nargs="+", help="关键词，可空格组合，如：小学 英语 教科版 五年级")
    p_search.add_argument("--limit", type=int, default=20, help="最多返回记录数（默认 20）")
    p_search.add_argument("--refresh", action="store_true", help="强制从云端刷新本地索引缓存")
    p_search.add_argument("--json", action="store_true", help="以 JSON 输出，便于程序处理")

    p_down = sub.add_parser("download", help="下载指定教材 PDF")
    p_down.add_argument("id", help="资源 contentId")
    p_down.add_argument("-o", "--output", help="输出文件路径（默认保存为书名.pdf）")
    p_down.add_argument("--token", help="Base64(JSON) 形式的 UC_TOKEN（未提供时读取环境变量 UC_TOKEN）")
    p_down.add_argument(
        "--mode", choices=["vector", "slices"], default="vector",
        help="vector=原版矢量 PDF（需 Token，默认）；slices=免登录切片合成 PDF",
    )

    p_audio = sub.add_parser("audio", help="下载教材配套听力音频")
    p_audio.add_argument("id", help="资源 contentId")
    p_audio.add_argument("-o", "--output", help="输出目录（默认 <书名>_audios）")
    p_audio.add_argument("--token", help="UC_TOKEN（未提供时读取环境变量 UC_TOKEN）")

    return parser


def main():
    args = build_parser().parse_args()
    try:
        if args.command == "search":
            cmd_search(args)
        elif args.command == "download":
            cmd_download(args)
        elif args.command == "audio":
            cmd_audio(args)
    except SmartEduError as e:
        print(f"\n错误：{e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
