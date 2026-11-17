"""平台适配器抽象（C11）：端口和适配器对得上、旧路径是同一模块、Agent / 知识库 / 工具层不直接依赖 eBay 模块。"""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "backend" / "app"


def test_adapters_implement_ports():
    from app.channels import MessagePort, OrderPort
    from app.channels.ebay.messages import EbayChannel
    from app.channels.ebay.orders import EbaySource
    from app.channels.mock import MockChannel
    from app.sources.fixture import FixtureSource

    assert issubclass(EbaySource, OrderPort) and issubclass(FixtureSource, OrderPort)
    assert isinstance(EbayChannel(), MessagePort) and isinstance(MockChannel("X-"), MessagePort)


def test_old_paths_are_aliases():
    import app.channels.ebay.orders as new
    import app.sources.ebay as old
    from app.channels import ports, registry
    from app.sending import channel
    from app.sources import base

    assert old is new  # 同一个模块对象：缓存、monkeypatch 都不受影响
    import app.channels.ebay.messages as msg_new

    assert channel is msg_new  # 对 app.sending.channel monkeypatch（如 check_public_url）照样生效
    assert base.BusinessSource is ports.OrderPort and channel.get_channel is registry.get_channel
    assert channel.ChannelUncertain is ports.ChannelUncertain and channel.SendReceipt is ports.SendReceipt


def test_registry_selects_by_store(app_env, monkeypatch):
    from app import sources
    from app.channels import get_message_port, get_order_port

    assert get_order_port("STORE_A").name == sources.get_business_source().name  # 演示店铺：配置的数据源
    monkeypatch.setattr("app.ebay.stores.is_ebay_store", lambda sid: True)
    assert get_order_port("ANY") is sources.get_ebay_source()
    assert get_message_port().name == "mock"


def _imports(path: Path) -> set[str]:
    mods = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add("." * node.level + node.module)
        elif isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
    return mods


def test_core_layers_depend_on_ports_only():
    """Agent、知识库、工具层不 import eBay 模块（ebay/、sources.ebay、channels.ebay）；ebay_replied 只读本地库，不算。"""
    bad = []
    for layer in ("agent", "knowledge", "tools"):
        for f in (APP / layer).glob("*.py"):
            for m in _imports(f):
                tail = m.lstrip(".")
                if tail.startswith(("ebay.", "sources.ebay", "channels.ebay")) or tail == "ebay" or tail.endswith(".sources"):
                    bad.append(f"{layer}/{f.name}: {m}")
    assert not bad, bad
