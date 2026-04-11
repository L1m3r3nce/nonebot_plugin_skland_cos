# nonebot-plugin-skland-cos

从森空岛社区获取明日方舟 COS 图并随机发送的 NoneBot2 插件。

## 安装

将 `nonebot_plugin_skland_cos` 目录复制到你的 NoneBot2 项目的插件目录下，或在 `pyproject.toml` 中添加:

```toml
plugins = ["nonebot_plugin_skland_cos"]
```

依赖: `httpx>=0.24.0`

```bash
pip install httpx
```

## 使用

| 指令 | 说明 |
|------|------|
| `/cos` | 随机发送一张明日方舟 COS 图 |
| `/cos 3` | 随机发送 3 张 (最多 9 张) |
| `/skcos` | 同 `/cos` |
| `/方舟cos` | 同 `/cos` |
| `/森空岛cos` | 同 `/cos` |

## API 说明

插件通过请求森空岛社区的 COS 板块帖子列表接口获取图片。

由于森空岛没有公开的社区帖子 API 文档，插件内置了多个候选 endpoint 并会依次尝试。如果全部失败，你需要自己抓包获取正确的 API 地址，然后修改 `__init__.py` 中的 `API_ENDPOINTS` 列表。

### 抓包方法

1. 打开浏览器 DevTools (F12) → Network 面板
2. 访问 `https://www.skland.com/game/arknights?cateId=3`
3. 筛选 XHR/Fetch 请求
4. 找到返回帖子列表 JSON 的请求
5. 将该 URL 填入 `API_ENDPOINTS`

## 许可

MIT
