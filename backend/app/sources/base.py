"""旧路径：接口已搬到 app/channels/ports.py（OrderPort，旧名 BusinessSource）。"""
from ..channels.ports import BusinessSource, OrderPort, SourceNotAvailable

__all__ = ["BusinessSource", "OrderPort", "SourceNotAvailable"]
