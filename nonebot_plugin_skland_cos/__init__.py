"""
NoneBot2 森空岛 COS 图插件
通过森空岛社区 API 获取明日方舟同人板块帖子中的图片并随机发送

使用:
  /cos            随机发送一张COS图
  /cos 3          随机发送3张COS图 (最多9张)
  /cos 阿米娅   搜索含关键词的COS图
  /cos 阿米娅 3  搜索关键词并发送3张

配置 (在 .env 中填写):
  SKLAND_CRED=你的森空岛 cred token
  SKLAND_DID=你的 Shumei 设备ID (可选，不填会用通用值)

获取方式: 浏览器登录 www.skland.com → F12 → Application → Local Storage:
  - SKLAND_CRED: 取 SK_OAUTH_CRED_KEY 的值
  - SKLAND_DID:  取 SK_SHUMEI_DEVICE_ID_KEY 的 id 字段的值
"""

import asyncio
import json
import hmac
import hashlib
import random
import string
import time
from pathlib import Path

import httpx
from nonebot import get_plugin_config, on_command, logger, get_driver
from nonebot.adapters.onebot.v11 import MessageSegment, Message, Bot, Event
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel


class Config(BaseModel):
    skland_cred: str = ""
    skland_did: str = ""


__plugin_meta__ = PluginMetadata(
    name="森空岛COS图",
    description="从森空岛社区获取明日方舟COS图并随机发送",
    usage="/cos  - 随机发送一张COS图\n/cos 3 - 随机发送3张COS图(最多9张)\n/cos 关键词 - 搜索含关键词的COS图\n/cos 关键词 3 - 搜索关键词并发送3张",
    type="application",
    homepage="https://pypi.org/project/nonebot-plugin-skland-cos/",
    supported_adapters={"~onebot.v11"},
    config=Config,
)

plugin_config = get_plugin_config(Config)

# ===================== 配置 =====================

BASE_URL = "https://zonai.skland.com"
GAME_ID = "1"   # 明日方舟
CATE_ID = "3"   # 同人板块
PAGE_SIZE = 50
TIMEOUT = 15
MAX_IMAGES = 9
PLATFORM = "3"
V_NAME = "1.0.0"

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.skland.com/",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.skland.com",
}

# 运行时缓存的签名 token（通过 /web/v1/auth/refresh 获取）
_sign_token: str = ""

# tag 名称 → tag ID 缓存，持久化文件
_TAG_CACHE_FILE = Path.cwd() / "data" / "skland_cos_tags.json"
_tag_cache: dict[str, int] = {}
_tag_cache_seeded: bool = False
_tag_scan_done: bool = False       # 初始 1-600 扫描是否完成
_tag_scan_max: int = 0             # 当前已扫描的最大 ID
_bg_scan_task: asyncio.Task | None = None  # 后台扫描 task 引用


# ===================== 签名工具 =====================

def _make_sign(token: str, did: str, path: str, method: str,
               query: str, body: str, ts: str) -> str:
    """
    Skland 请求签名算法（逆向自前端 JS）:
    sign = MD5( HMAC-SHA256(token, path + query/body + ts + JSON_headers) )
    JSON_headers = {"platform":"3","timestamp":ts,"dId":did,"vName":"1.0.0"}
    """
    msg = path
    msg += (query or "") if method.upper() == "GET" else (body or "")
    msg += ts
    hdr: dict = {}
    for k, v in [("platform", PLATFORM), ("timestamp", ts), ("dId", did), ("vName", V_NAME)]:
        if v:
            hdr[k] = v
    msg += json.dumps(hdr, separators=(",", ":"))
    raw = hmac.new(token.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hashlib.md5(raw.encode()).hexdigest()


async def _refresh_token(client: httpx.AsyncClient, cred: str) -> str:
    """调用 /web/v1/auth/refresh 获取新的 sign token"""
    global _sign_token
    try:
        resp = await client.get(
            f"{BASE_URL}/web/v1/auth/refresh",
            headers={**_BASE_HEADERS, "cred": cred},
            timeout=TIMEOUT,
        )
        data = resp.json()
        if data.get("code") == 0:
            _sign_token = data["data"]["token"]
            logger.info(f"[skland_cos] sign token 刷新成功: {_sign_token[:12]}...")
        else:
            logger.warning(f"[skland_cos] token 刷新失败: {data.get('message')}")
    except Exception as e:
        logger.warning(f"[skland_cos] token 刷新异常: {e}")
    return _sign_token


async def _signed_get(client: httpx.AsyncClient, cred: str, did: str,
                      path: str, params: dict) -> dict:
    """发送带签名的 GET 请求，token 过期时自动刷新一次"""
    global _sign_token

    async def _do_request(token: str) -> httpx.Response:
        req = client.build_request("GET", f"{BASE_URL}{path}", params=params)
        query = str(req.url).split("?", 1)[1] if "?" in str(req.url) else ""
        ts = str(int(time.time()))
        sign = _make_sign(token, did, path, "GET", query, "", ts)
        headers = {
            **_BASE_HEADERS,
            "cred": cred,
            "platform": PLATFORM,
            "vName": V_NAME,
            "timestamp": ts,
            "dId": did,
            "sign": sign,
        }
        return await client.get(f"{BASE_URL}{path}", params=params, headers=headers)

    if not _sign_token:
        await _refresh_token(client, cred)

    resp = await _do_request(_sign_token)
    data = resp.json()

    # code=10000 表示 token 失效，刷新后重试一次
    if data.get("code") == 10000:
        logger.info("[skland_cos] token 失效，自动刷新重试")
        await _refresh_token(client, cred)
        resp = await _do_request(_sign_token)
        data = resp.json()

    return data


driver = get_driver()


@driver.on_startup
async def _init_token() -> None:
    cred = plugin_config.skland_cred
    if not cred:
        logger.warning("[skland_cos] 未配置 SKLAND_CRED，插件将无法工作")
        return
    _load_tag_cache()
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as client:
        await _refresh_token(client, cred)
        await _seed_tag_cache(client, cred, plugin_config.skland_did or "")
    # 后台全量扫描 tag ID 1-600，不阻塞启动
    global _bg_scan_task
    _bg_scan_task = asyncio.create_task(_bg_scan_tag_ids(cred, plugin_config.skland_did or "", 1, 600))


# ===================== API 层 =====================

_COSPLAY_TAG_ID = 451  # 明日方舟 cosplay 板块 tagId


def _random_list_id() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=16))


# ── tag 缓存持久化 ──

def _load_tag_cache() -> None:
    try:
        if _TAG_CACHE_FILE.exists():
            data = json.loads(_TAG_CACHE_FILE.read_text(encoding="utf-8"))
            _tag_cache.update({k.lower(): int(v) for k, v in data.items()})
            logger.info(f"[skland_cos] 从文件加载 {len(_tag_cache)} 个 tag 缓存")
    except Exception as e:
        logger.warning(f"[skland_cos] 加载 tag 缓存文件失败: {e}")


def _save_tag_cache() -> None:
    try:
        _TAG_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TAG_CACHE_FILE.write_text(
            json.dumps(_tag_cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"[skland_cos] 保存 tag 缓存文件失败: {e}")


def _absorb_tags(tags: list) -> None:
    for t in tags:
        if isinstance(t, dict):
            tid = t.get("id")
            name = t.get("name", "")
            if tid and name:
                _tag_cache[name.lower()] = int(tid)


def _extract_images_from_post_list(
    post_list: list, seen_urls: set[str] | None = None
) -> list[dict]:
    """从 tag/index 帖子列表提取图片，同时更新 tag 缓存。seen_urls 用于跨页去重。"""
    result: list[dict] = []
    if seen_urls is None:
        seen_urls = set()
    for entry in post_list:
        if not isinstance(entry, dict):
            continue
        _absorb_tags(entry.get("tags", []))
        user = entry.get("user", {})
        author = user.get("nickname", user.get("name", "")) if isinstance(user, dict) else ""
        item = entry.get("item", {})
        if not isinstance(item, dict):
            continue
        post_id = item.get("id", "")
        post_url = f"https://www.skland.com/article?id={post_id}" if post_id else ""
        title = item.get("title", "") or ""
        tag_names = [t.get("name", "") for t in entry.get("tags", []) if isinstance(t, dict)]
        for img in item.get("imageListSlice", []):
            url = img.get("url", "") if isinstance(img, dict) else str(img)
            if url and url.startswith("http") and url not in seen_urls:
                seen_urls.add(url)
                result.append({
                    "url": url,
                    "author": author,
                    "post_url": post_url,
                    "title": title,
                    "tags": tag_names,
                })
    return result


async def _fetch_tag_index_page(
    client: httpx.AsyncClient, cred: str, did: str,
    tag_id: int, list_id: str, sort_type: str = "1",
) -> tuple[list[dict], bool]:
    data = await _signed_get(
        client, cred, did,
        "/web/v1/tag/index",
        {"tagId": str(tag_id), "sortType": sort_type,
         "pageSize": "10", "listId": list_id, "gameId": "0"},
    )
    if data.get("code") != 0:
        return [], False
    payload = data.get("data", {})
    return payload.get("list", []), bool(payload.get("hasMore"))


async def _lookup_tag_name(
    client: httpx.AsyncClient, cred: str, did: str, tid: int
) -> str:
    """单次查询某个 tag ID 对应的名字"""
    data = await _signed_get(client, cred, did, "/web/v1/tag", {"id": str(tid), "gameId": GAME_ID})
    if data.get("code") == 0:
        return data.get("data", {}).get("tagAgg", {}).get("tag", {}).get("name", "")
    return ""


async def _seed_tag_cache(client: httpx.AsyncClient, cred: str, did: str) -> None:
    """快速预热（阻塞，在启动时同步完成）：
    1. cosplay 板块拉 3 页，从帖子顶层 tags 字段获取名字→ID
    2. feed/index 最近帖子 tagIdsSlice 逐个查名字"""
    global _tag_cache_seeded
    if _tag_cache_seeded:
        return

    list_id = _random_list_id()
    for _ in range(3):
        posts, has_more = await _fetch_tag_index_page(client, cred, did, _COSPLAY_TAG_ID, list_id)
        if not posts:
            break
        _extract_images_from_post_list(posts)
        if not has_more:
            break

    feed = await _signed_get(
        client, cred, did,
        "/web/v1/feed/index",
        {"gameId": GAME_ID, "cateId": CATE_ID, "limit": "50"},
    )
    raw_posts = feed.get("data", {}).get("list", []) if feed.get("code") == 0 else []
    tid_set: set[int] = set()
    for p in raw_posts:
        tid_set.update(p.get("item", {}).get("tagIdsSlice", []))

    known_ids = set(_tag_cache.values())
    for tid in tid_set - known_ids:
        name = await _lookup_tag_name(client, cred, did, tid)
        if name:
            _tag_cache[name.lower()] = tid

    _tag_cache_seeded = True
    logger.info(f"[skland_cos] tag 缓存预热完成，已知 {len(_tag_cache)} 个 tag")
    _save_tag_cache()


async def _bg_scan_tag_ids(cred: str, did: str, start: int, end: int) -> None:
    """后台并发扫描 tag ID [start, end]，并发度 10，结果持久化到文件"""
    global _tag_scan_done, _tag_scan_max
    logger.info(f"[skland_cos] 开始后台扫描 tag ID {start}-{end} ...")
    sem = asyncio.Semaphore(10)
    known_ids = set(_tag_cache.values())

    async def probe(client: httpx.AsyncClient, tid: int) -> None:
        async with sem:
            if tid in known_ids:
                return
            try:
                name = await _lookup_tag_name(client, cred, did, tid)
                if name:
                    _tag_cache[name.lower()] = tid
                    known_ids.add(tid)
            except Exception:
                pass
            if tid > _tag_scan_max:
                _tag_scan_max = tid

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as client:
            await _refresh_token(client, cred)
            await asyncio.gather(*[probe(client, tid) for tid in range(start, end + 1)])
    except Exception as e:
        logger.warning(f"[skland_cos] 后台扫描异常: {e}")

    if end >= 600:
        _tag_scan_done = True
    logger.info(f"[skland_cos] 后台扫描 {start}-{end} 完成，tag 缓存共 {len(_tag_cache)} 个")
    _save_tag_cache()


def _lookup_tag_id(keyword: str) -> int | None:
    kw = keyword.lower().strip()
    if kw in _tag_cache:
        return _tag_cache[kw]
    for name, tid in _tag_cache.items():
        if kw in name:
            return tid
    return None


async def _resolve_unknown_tag(
    client: httpx.AsyncClient, cred: str, did: str, keyword: str
) -> int | None:
    """当 keyword 在缓存中找不到时，尝试以下策略直到命中或放弃：
    1. 若后台扫描未完成，等待最多 8 秒让扫描跑完再查
    2. 若扫描已完成但仍未找到，把扫描范围扩展到 1200 并等待最多 10 秒
    3. 找到后写入缓存文件；始终无果则返回 None"""
    global _bg_scan_task, _tag_scan_done, _tag_scan_max

    # ── 策略 1：等待正在进行的初始扫描（最多 8 秒）──
    if not _tag_scan_done and _bg_scan_task is not None:
        logger.info(f"[skland_cos] 关键词={keyword!r} 缓存未命中，等待后台扫描完成...")
        try:
            await asyncio.wait_for(asyncio.shield(_bg_scan_task), timeout=8)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        tag_id = _lookup_tag_id(keyword)
        if tag_id is not None:
            return tag_id

    # ── 策略 2：初始扫描完成但仍未找到，扩展到 1200 ──
    if _tag_scan_done and _tag_scan_max < 1200:
        logger.info(f"[skland_cos] 关键词={keyword!r} 扩展扫描 {_tag_scan_max + 1}-1200 ...")
        ext_task = asyncio.create_task(
            _bg_scan_tag_ids(cred, did, _tag_scan_max + 1, 1200)
        )
        try:
            await asyncio.wait_for(asyncio.shield(ext_task), timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        tag_id = _lookup_tag_id(keyword)
        if tag_id is not None:
            return tag_id

    # ── 策略 3：前两步都没找到，从最新 feed/index 补充一轮 tag 名字 ──
    feed = await _signed_get(
        client, cred, did,
        "/web/v1/feed/index",
        {"gameId": GAME_ID, "cateId": CATE_ID, "limit": "50"},
    )
    raw_posts = feed.get("data", {}).get("list", []) if feed.get("code") == 0 else []
    known_ids = set(_tag_cache.values())
    for p in raw_posts:
        for tid in p.get("item", {}).get("tagIdsSlice", []):
            if tid not in known_ids:
                name = await _lookup_tag_name(client, cred, did, tid)
                if name:
                    _tag_cache[name.lower()] = tid
                    known_ids.add(tid)

    tag_id = _lookup_tag_id(keyword)
    if tag_id is not None:
        _save_tag_cache()
    return tag_id


async def fetch_cos_images(keyword: str = "") -> list[dict]:
    """
    从森空岛同人板块拉取帖子图片
    keyword: 可选关键词，先从 tag 缓存查 tagId 走角色板块，未命中则 feed/index 文本匹配
    返回: [{"url": str, "author": str, "post_url": str, "title": str, "tags": list}, ...]
    """
    cred = plugin_config.skland_cred
    if not cred:
        return []

    did = plugin_config.skland_did or ""

    async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as client:
        # ── 有关键词：查 tag 缓存 ──
        if keyword:
            tag_id = _lookup_tag_id(keyword)

            # 缓存未命中：尝试找到该 tag 并加入缓存
            if tag_id is None:
                tag_id = await _resolve_unknown_tag(client, cred, did, keyword)

            if tag_id is not None:
                logger.info(f"[skland_cos] 关键词={keyword!r} → tagId={tag_id}，使用 tag/index")
                seen: set[str] = set()
                result: list[dict] = []
                list_id = _random_list_id()
                for _ in range(5):
                    posts, has_more = await _fetch_tag_index_page(
                        client, cred, did, tag_id, list_id
                    )
                    if not posts:
                        break
                    result.extend(_extract_images_from_post_list(posts, seen))
                    if not has_more:
                        break
                # 叠加 feed/index 标题匹配，补充没有打标签的帖子
                result.extend(await _feed_title_search(client, cred, did, keyword, seen))
                if result:
                    logger.info(f"[skland_cos] tag={tag_id} 共获取 {len(result)} 张图片")
                    return result
                logger.info("[skland_cos] tag/index 无图片，回落到 feed/index")

        # ── 无关键词：cosplay 板块用 sortType=2（最新）保证每次不重复 ──
        if not keyword:
            seen = set()
            result = []
            list_id = _random_list_id()
            for _ in range(5):
                posts, has_more = await _fetch_tag_index_page(
                    client, cred, did, _COSPLAY_TAG_ID, list_id, sort_type="2"
                )
                if not posts:
                    break
                result.extend(_extract_images_from_post_list(posts, seen))
                if not has_more:
                    break
            if result:
                logger.info(f"[skland_cos] 无关键词 cosplay tag 共获取 {len(result)} 张图片")
                return result

        # ── 有关键词但 tag 完全未命中：纯 feed/index 标题匹配 ──
        result = await _feed_title_search(client, cred, did, keyword)

    logger.info(f"[skland_cos] 关键词={keyword!r} 共获取 {len(result)} 张图片")
    return result


async def _feed_title_search(
    client: httpx.AsyncClient, cred: str, did: str,
    keyword: str, seen: set[str] | None = None,
) -> list[dict]:
    """从 feed/index 拉取帖子，按标题/作者匹配 keyword，返回去重后的图片列表"""
    if seen is None:
        seen = set()
    data = await _signed_get(
        client, cred, did,
        "/web/v1/feed/index",
        {"gameId": GAME_ID, "cateId": CATE_ID, "limit": str(PAGE_SIZE * 2)},
    )
    if data.get("code") != 0:
        return []

    result: list[dict] = []
    kw = keyword.lower()
    for entry in data.get("data", {}).get("list", []):
        user = entry.get("user", {})
        author = user.get("nickname", user.get("name", "")) if isinstance(user, dict) else ""
        item = entry.get("item", {})
        if not isinstance(item, dict):
            continue
        title = item.get("title", "") or ""
        content = item.get("content", "") or ""
        if isinstance(content, dict):
            content = content.get("text", content.get("html", "")) or ""
        if not (kw in title.lower() or kw in author.lower() or kw in content.lower()):
            continue
        post_id = item.get("id", "")
        post_url = f"https://www.skland.com/article?id={post_id}" if post_id else ""
        for img in item.get("imageListSlice", []):
            url = img.get("url", "") if isinstance(img, dict) else str(img)
            if url and url.startswith("http") and url not in seen:
                seen.add(url)
                result.append({
                    "url": url,
                    "author": author,
                    "post_url": post_url,
                    "title": title,
                    "tags": [],
                })
    return result


# ===================== 指令 =====================

cos_cmd = on_command(
    "cos",
    aliases={"skcos", "方舟cos", "森空岛cos"},
    priority=10,
    block=True,
)


@cos_cmd.handle()
async def handle_cos(bot: Bot, event: Event, args: Message = CommandArg()) -> None:
    num = 1
    keyword = ""
    text = args.extract_plain_text().strip()

    if text:
        parts = text.split()
        # 最后一个 token 是纯数字则视为数量，其余为关键词
        if parts[-1].isdigit():
            num = int(parts[-1])
            keyword = " ".join(parts[:-1])
        else:
            keyword = text

    num = max(1, min(num, MAX_IMAGES))

    hint = f"正在从森空岛搜索「{keyword}」的COS图, 请稍候~" if keyword else "正在从森空岛获取COS图, 请稍候~"
    await cos_cmd.send(hint)

    try:
        images = await fetch_cos_images(keyword)
    except Exception as e:
        logger.exception("[skland_cos] fetch error")
        await cos_cmd.finish(f"获取失败: {e}")
        return

    if not images:
        if not plugin_config.skland_cred:
            await cos_cmd.finish(
                "未配置森空岛账号\n"
                "请在 .env 中添加:\n"
                "  SKLAND_CRED=SK_OAUTH_CRED_KEY 的值\n"
                "  SKLAND_DID=SK_SHUMEI_DEVICE_ID_KEY 的 id 字段\n"
                "获取方式: 登录 www.skland.com → F12 → Application → Local Storage"
            )
        elif keyword:
            hint = ""
            if not _tag_scan_done:
                hint = "\n（正在建立角色索引，请 30 秒后再试）"
            elif _tag_scan_max < 1200:
                hint = "\n（已扩展搜索范围，请稍后再试）"
            await cos_cmd.finish(f"没有找到「{keyword}」相关的COS图 orz{hint}")
        else:
            await cos_cmd.finish(
                "没有获取到COS图 orz\n"
                "可能是 cred 已过期，请重新获取 SKLAND_CRED"
            )
        return

    selected = random.sample(images, min(num, len(images)))

    async def _download_image(url: str) -> bytes | None:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as c:
                r = await c.get(url, headers={"Referer": "https://www.skland.com/"})
                if r.status_code == 200:
                    return r.content
        except Exception:
            pass
        return None

    if num == 1:
        img = selected[0]
        data = await _download_image(img["url"])
        parts: list[str] = []
        if img["author"]:
            parts.append(f"作者: {img['author']}")
        if img["post_url"]:
            parts.append(img["post_url"])
        footer = ("\n" + "\n".join(parts)) if parts else ""
        if data:
            msg = MessageSegment.image(data)
            if footer:
                msg += MessageSegment.text(footer)
            await cos_cmd.finish(msg)
        else:
            await cos_cmd.finish(f"图片获取失败，链接: {img['url']}{footer}")
    else:
        for img in selected:
            data = await _download_image(img["url"])
            if data:
                await cos_cmd.send(MessageSegment.image(data))
            else:
                await cos_cmd.send(f"[图片获取失败] {img['url']}")
        authors = list({i["author"] for i in selected if i["author"]})
        footer = "来源: 森空岛 同人板块"
        if authors:
            footer += f" | {', '.join(authors[:3])}"
        await cos_cmd.finish(footer)
