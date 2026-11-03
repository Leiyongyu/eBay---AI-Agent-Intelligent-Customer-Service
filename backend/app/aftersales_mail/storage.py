"""售后附件存储：按店铺分文件夹、“订单号_时间_序号”命名，类型检查、大小上限、病毒扫描。"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..config import get_settings
from .mailbox import safe_name

UNCLAIMED_DIR = "_待认领"
ALLOWED_EXT = {
    # 图片
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff",
    # 视频、音频
    ".mp4", ".m4v", ".mov", ".webm", ".avi", ".mkv", ".3gp", ".wmv", ".mpg", ".mpeg", ".m4a", ".mp3", ".wav", ".ogg",
    # 文档、压缩包（压缩包同样要过病毒扫描）
    ".pdf", ".txt", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip", ".rar", ".7z",
}
# 浏览器能直接放的视频；其余（MOV/HEVC 等）只提供下载
PLAYABLE_VIDEO = {".mp4", ".m4v", ".webm"}
PREVIEW_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class FileTooLarge(Exception):
    pass


@dataclass
class Saved:
    rel_path: str
    size: int
    sha256: str
    content_type: str


def max_bytes() -> int:
    return get_settings().aftersales_file_max_mb * 1024 * 1024


def ext_of(filename: str | None, content_type: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    if not ext and content_type:
        ext = (mimetypes.guess_extension(content_type) or "").lower()
    return ".jpg" if ext == ".jpe" else ext


def allowed(ext: str) -> bool:
    return ext in ALLOWED_EXT


def store_dir(store_id: str | None, ebay_user_id: str | None) -> str:
    if not store_id:
        return UNCLAIMED_DIR
    return safe_name(f"{store_id}（{ebay_user_id}）" if ebay_user_id else store_id)


def file_name(order_id: str | None, when: datetime, seq: int, ext: str) -> str:
    return f"{safe_name(order_id) if order_id else '未匹配'}_{when:%Y%m%d-%H%M%S}_{seq:02d}{ext}"


def _unique(path: Path) -> Path:
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return candidate


def save_stream(chunks: Iterable[bytes], folder: str, name: str, content_type: str) -> Saved:
    """边写边算大小和指纹，超过上限立刻停止并删掉半截文件。"""
    root = get_settings().aftersales_root
    path = _unique(root / folder / name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    h, size, limit = hashlib.sha256(), 0, max_bytes()
    try:
        with open(tmp, "wb") as f:
            for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise FileTooLarge()
                h.update(chunk)
                f.write(chunk)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return Saved(path.relative_to(root).as_posix(), size, h.hexdigest(), content_type)


def move(rel_path: str, folder: str, name: str) -> str:
    """认领后挪到店铺文件夹并改名，返回新相对路径。"""
    root = get_settings().aftersales_root
    src = root / rel_path
    dst = _unique(root / folder / name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst.relative_to(root).as_posix()


def remove_empty_dirs() -> int:
    root = get_settings().aftersales_root
    n = 0
    if not root.exists():
        return 0
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
            n += 1
    return n


def _defender() -> str | None:
    path = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"
    return str(path) if path.exists() else None


def scan(rel_path: str) -> tuple[str, str | None]:
    """Windows Defender 扫描单个文件，返回 (结果, 说明)：CLEAN / THREAT / SKIPPED / ERROR。
    只有扫描输出明确报告发现威胁（或文件被实时防护隔离）才算 THREAT 并删文件；
    Defender 服务没运行、扫描失败等一律不删，记 SKIPPED / ERROR 给人看。"""
    if not get_settings().aftersales_virus_scan:
        return "SKIPPED", "未开启病毒扫描（AFTERSALES_VIRUS_SCAN=false）"
    exe = _defender()
    path = get_settings().aftersales_root / rel_path
    if exe is None:
        return "SKIPPED", "本机没有 Windows Defender，未做病毒扫描"
    if not path.exists():
        return "THREAT", "文件写入后被杀毒软件实时防护隔离"
    try:
        r = subprocess.run([exe, "-Scan", "-ScanType", "3", "-File", str(path), "-DisableRemediation"],
                           capture_output=True, timeout=1800,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        return "ERROR", f"病毒扫描没有完成：{type(e).__name__}"
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    if "0x800106ba" in out.lower():
        return "SKIPPED", "Windows Defender 服务没有运行，未做病毒扫描"
    found = re.search(r"found (\d+) threats?", out, re.I)
    if (found and int(found.group(1)) > 0) or not path.exists():
        path.unlink(missing_ok=True)
        return "THREAT", "病毒扫描发现威胁，文件已删除"
    if r.returncode == 0 or re.search(r"found no threats", out, re.I):
        return "CLEAN", None
    m = re.search(r"hr\s*=\s*(0x[0-9a-f]+)", out, re.I)
    return "ERROR", f"病毒扫描失败（{m.group(1) if m else f'返回码 {r.returncode}'}），打开前请注意"
