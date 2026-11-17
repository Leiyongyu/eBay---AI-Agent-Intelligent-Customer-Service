"""旧路径：消息渠道已收口到 app/channels/（ports 接口、mock 本地模拟、ebay/messages eBay 适配器、registry 选渠道）。
这里是 app.channels.ebay.messages 的别名（同一模块对象：对旧路径 monkeypatch check_public_url 等照样生效），旧导入照常可用。"""
import sys

from ..channels.ebay import messages as _messages

sys.modules[__name__] = _messages
