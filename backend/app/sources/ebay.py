"""旧路径：eBay 数据源已搬到 app/channels/ebay/orders.py（OrderPort 的 eBay 适配器）。这里只是同一模块的别名，旧的导入照常可用。"""
import sys

from ..channels.ebay import orders as _orders

sys.modules[__name__] = _orders
