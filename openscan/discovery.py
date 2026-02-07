"""Unified scanner discovery across all protocols."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from .scanner.base import ScannerInfo

logger = logging.getLogger(__name__)


def discover_all(timeout: float = 5.0, protocols: Optional[list[str]] = None) -> list[ScannerInfo]:
    """Discover scanners using all available protocols in parallel.

    Args:
        timeout: Discovery timeout per protocol in seconds.
        protocols: List of protocols to use. Default: all available.

    Returns:
        Deduplicated list of discovered scanners.
    """
    if protocols is None:
        protocols = ["escl", "wsd"]

    scanners: list[ScannerInfo] = []
    futures = {}

    with ThreadPoolExecutor(max_workers=len(protocols)) as pool:
        for proto in protocols:
            if proto == "escl":
                from .scanner.escl import EsclScanner
                futures[pool.submit(EsclScanner().discover, timeout)] = "escl"
            elif proto == "wsd":
                from .scanner.wsd import WsdScanner
                futures[pool.submit(WsdScanner().discover, timeout)] = "wsd"

        for future in as_completed(futures, timeout=timeout + 5):
            proto = futures[future]
            try:
                found = future.result()
                logger.info("Found %d scanners via %s", len(found), proto)
                scanners.extend(found)
            except Exception as e:
                logger.warning("Discovery failed for %s: %s", proto, e)

    return _deduplicate(scanners)


def probe_scanner(ip: str, port: int = 80, protocol: str = "escl") -> Optional[ScannerInfo]:
    """Probe a specific IP/port for a scanner.

    Used for manual IP entry in the wizard.
    """
    if protocol == "escl":
        from .scanner.escl import EsclScanner
        scanner = ScannerInfo(
            name=f"Scanner at {ip}",
            ip=ip,
            port=port,
            protocol="escl",
        )
        escl = EsclScanner()
        if escl.test_connection(scanner):
            try:
                caps = escl.get_capabilities(scanner)
                scanner.sources = caps.sources
            except Exception:
                pass
            return scanner

    elif protocol == "wsd":
        from .scanner.wsd import WsdScanner
        scanner = ScannerInfo(
            name=f"Scanner at {ip}",
            ip=ip,
            port=port,
            protocol="wsd",
        )
        wsd = WsdScanner()
        if wsd.test_connection(scanner):
            try:
                caps = wsd.get_capabilities(scanner)
                scanner.sources = caps.sources
            except Exception:
                pass
            return scanner

    return None


def _deduplicate(scanners: list[ScannerInfo]) -> list[ScannerInfo]:
    """Remove duplicate scanners found via multiple protocols.

    Prefer eSCL over WSD when the same device is found via both.
    """
    seen_ips: dict[str, ScannerInfo] = {}
    for s in scanners:
        key = s.ip
        if key in seen_ips:
            existing = seen_ips[key]
            # Prefer eSCL
            if s.protocol == "escl" and existing.protocol != "escl":
                seen_ips[key] = s
        else:
            seen_ips[key] = s

    return list(seen_ips.values())
