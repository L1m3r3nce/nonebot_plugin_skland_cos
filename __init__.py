"""
NoneBot2 森空岛 COS 图插件
通过森空岛社区 API 获取明日方舟同人板块帖子中的图片并随机发送

使用:
  /cos       随机发送一张COS图
  /cos 3     随机发送3张COS图 (最多9张)

配置 (在 .env 中填写):
  SKLAND_CRED=你的森空岛 cred token
  SKLAND_DID=你的 Shumei 设备ID (可选，不填会用通用值)

获取方式: 浏览器登录 www.skland.com → F12 → Application → Local Storage:
  - SKLAND_CRED: 取 SK_OAUTH_CRED_KEY 的值
  - SKLAND_DID:  取 SK_SHUMEI_DEVICE_ID_KEY 的 id 字段的值
"""

import json
import hmac
import hashlib
import random
import time

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
    usage="/cos  - 随机发送一张COS图\n/cos 3 - 随机发送3张COS图(最多9张)",
    type="application",
    homepage="",
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
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as client:
        await _refresh_token(client, cred)


# ===================== API 层 =====================

async def fetch_cos_images() -> list[dict]:
    """
    从森空岛同人板块拉取帖子图片
    返回: [{"url": str, "author": str, "post_url": str}, ...]
    """
    cred = plugin_config.skland_cred
    if not cred:
        return []

    did = plugin_config.skland_did or ""

    result: list[dict] = []
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as client:
        data = await _signed_get(
            client, cred, did,
            "/web/v1/feed/index",
            {"gameId": GAME_ID, "cateId": CATE_ID, "limit": str(PAGE_SIZE)},
        )

        if data.get("code") != 0:
            logger.warning(f"[skland_cos] API 返回 code={data.get('code')} msg={data.get('message')}")
            return []

        payload = data.get("data", {})
        post_list = []
        for key in ("list", "posts", "items"):
            v = payload.get(key)
            if isinstance(v, list) and v:
                post_list = v
                break

        for entry in post_list:
            user = entry.get("user", {})
            author = user.get("nickname", user.get("name", ""))
            item = entry.get("item", {})
            post_id = item.get("id", "")
            post_url = f"https://www.skland.com/article?id={post_id}" if post_id else ""

            # 图片在 imageListSlice 字段
            img_slice = item.get("imageListSlice", [])
            for img in img_slice:
                url = img.get("url", "") if isinstance(img, dict) else str(img)
                if url and url.startswith("http"):
                    result.append({"url": url, "author": author, "post_url": post_url})

    logger.info(f"[skland_cos] 共获取 {len(result)} 张图片")
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
    text = args.extract_plain_text().strip()
    if text:
        try:
            num = int(text)
        except ValueError:
            await cos_cmd.finish("请输入数字, 例如: /cos 3")
            return
    num = max(1, min(num, MAX_IMAGES))

    await cos_cmd.send("正在从森空岛获取COS图, 请稍候~")

    try:
        images = await fetch_cos_images()
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
        else:
            await cos_cmd.finish(
                "没有获取到COS图 orz\n"
                "可能是 cred 已过期，请重新获取 SKLAND_CRED"
            )
        return

    selected = random.sample(images, min(num, len(images)))

    if num == 1:
        img = selected[0]
        msg = MessageSegment.image(img["url"])
        parts: list[str] = []
        if img["author"]:
            parts.append(f"作者: {img['author']}")
        if img["post_url"]:
            parts.append(img["post_url"])
        if parts:
            msg += MessageSegment.text("\n" + "\n".join(parts))
        try:
            await cos_cmd.finish(msg)
        except Exception:
            await cos_cmd.finish(f"图片发送失败，链接: {img['url']}")
    else:
        for img in selected:
            try:
                await cos_cmd.send(MessageSegment.image(img["url"]))
            except Exception:
                await cos_cmd.send(f"[图片发送失败] {img['url']}")
        authors = list({i["author"] for i in selected if i["author"]})
        footer = "来源: 森空岛 同人板块"
        if authors:
            footer += f" | {', '.join(authors[:3])}"
        await cos_cmd.finish(footer)
