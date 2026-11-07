"""Email destination: send each scan as a PDF attachment over SMTP.

The password is never logged. A scan above the size limit fails with a plain
message, so the retry queue keeps it.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from datetime import datetime
from email.message import EmailMessage

from .base import DeliveryError, Destination, field

logger = logging.getLogger(__name__)

_TIMEOUT = 30
_SECURITY = ["starttls", "ssl", "none"]
_DEFAULT_PORT = {"starttls": 587, "ssl": 465, "none": 25}


def _recipients(value) -> list[str]:
    if isinstance(value, str):
        value = value.replace(";", ",").replace("\n", ",").split(",")
    out = [str(v).strip() for v in (value or []) if str(v).strip()]
    for addr in out:
        if "@" not in addr or any(c in addr for c in "\r\n "):
            raise DeliveryError(f"{addr!r} is not a valid email address.")
    return out


def _settings(options: dict) -> dict:
    host = str(options.get("host") or "").strip()
    if not host:
        raise DeliveryError("No mail server is set.")
    security = str(options.get("security") or "starttls")
    if security not in _SECURITY:
        raise DeliveryError("Mail security must be starttls, ssl or none.")
    try:
        port = int(options.get("port") or _DEFAULT_PORT[security])
    except (TypeError, ValueError):
        raise DeliveryError("The mail server port must be a number.") from None
    try:
        max_mb = float(options.get("max_attachment_mb") or 20)
    except (TypeError, ValueError):
        raise DeliveryError("The maximum attachment size must be a number of MB.") from None
    return {"host": host, "port": port, "security": security,
            "username": str(options.get("username") or ""),
            "password": str(options.get("password") or ""),
            "sender": str(options.get("from") or "").strip(),
            "max_bytes": int(max_mb * 1024 * 1024), "max_mb": max_mb}


def _connect(s: dict) -> smtplib.SMTP:
    if s["security"] == "ssl":
        smtp = smtplib.SMTP_SSL(s["host"], s["port"], timeout=_TIMEOUT,
                                context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(s["host"], s["port"], timeout=_TIMEOUT)
    try:
        if s["security"] == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if s["username"]:
            smtp.login(s["username"], s["password"])
    except Exception:
        try:
            smtp.close()
        except Exception:
            pass
        raise
    return smtp


def _smtp_error(e: Exception, s: dict) -> str:
    host = s["host"]
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return f"The mail server {host} rejected the user name or password."
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return f"The mail server {host} refused the recipient address."
    if isinstance(e, smtplib.SMTPSenderRefused):
        return f"The mail server {host} refused the sender address."
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return f"The mail server {host} does not support the chosen security. Try another security setting."
    if isinstance(e, smtplib.SMTPDataError):
        return f"The mail server {host} refused the message. It may be too large."
    if isinstance(e, ssl.SSLCertVerificationError):
        return f"The certificate of {host} is not trusted."
    if isinstance(e, (ssl.SSLError, smtplib.SMTPServerDisconnected)):
        return (f"Could not make a secure connection to {host}. "
                "Check the port and the security setting.")
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f"The mail server {host} did not answer in time."
    if isinstance(e, smtplib.SMTPException):
        return f"The mail server {host} reported an error."
    return f"Could not reach the mail server {host}. Check the name and the port."


def _value_error(e: ValueError, s: dict) -> str:
    """Plain text for a ValueError from smtplib. Never includes the error
    text, which can hold the password."""
    if isinstance(e, UnicodeError):
        return (f"The mail server {s['host']} cannot take a character in the user name or "
                "password. Use only plain ASCII letters, digits and symbols, or an app password.")
    return f"The mail settings for {s['host']} are not valid. Check the server, port and addresses."


def _subject(template: str, filename: str) -> str:
    class _Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    now = datetime.now()
    try:
        text = (template or "Scan: {filename}").format_map(_Safe(
            filename=filename, date=now.strftime("%Y-%m-%d"), time=now.strftime("%H:%M")))
    except (ValueError, IndexError, KeyError, AttributeError):
        text = template
    return text.replace("\r", " ").replace("\n", " ")


class EmailDestination(Destination):
    type = "email"
    label = "Email"
    feature = None
    target_key = "to"
    FIELDS = [
        field("host", "Mail server", "text", required=True),
        field("port", "Port", "text", default="587"),
        field("security", "Security", "choice", default="starttls", choices=_SECURITY),
        field("username", "User name", "text"),
        field("password", "Password", "password"),
        field("from", "From address", "text", required=True),
        field("to", "Send to", "list", required=True),
        field("subject", "Subject (use {filename}, {date}, {time})", "text",
              default="Scan: {filename}"),
        field("max_attachment_mb", "Largest attachment (MB)", "text", default="20"),
    ]

    @classmethod
    def target(cls, options: dict) -> str:
        to = (options or {}).get("to") or ""
        return ", ".join(to) if isinstance(to, list) else str(to)

    def deliver(self, pdf: bytes, filename: str, options: dict) -> tuple[str, bool]:
        s = _settings(options)
        to = _recipients(options.get("to"))
        if not to:
            raise DeliveryError("No email recipient is set.")
        if not s["sender"] or "@" not in s["sender"]:
            raise DeliveryError("The From address is not set or is not valid.")
        if len(pdf) > s["max_bytes"]:
            raise DeliveryError(
                f"The scan is {len(pdf) / 1048576:.1f} MB, above the {s['max_mb']:g} MB email limit. "
                "OpenScanHub keeps it and will try again. Raise the limit or use another destination.")
        msg = EmailMessage()
        msg["From"] = s["sender"]
        msg["To"] = ", ".join(to)
        msg["Subject"] = _subject(str(options.get("subject") or ""), filename)
        msg.set_content(f"The scan {filename} is attached.")
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=filename)
        try:
            smtp = _connect(s)
            try:
                smtp.send_message(msg)
            finally:
                try:
                    smtp.quit()
                except Exception:
                    pass
        except DeliveryError:
            raise
        except (smtplib.SMTPException, OSError) as e:
            logger.error("Email delivery via %s failed: %s", s["host"], type(e).__name__)
            raise DeliveryError(_smtp_error(e, s)) from e
        except ValueError as e:  # UnicodeError too: a non-ASCII password in AUTH
            logger.error("Email delivery via %s failed: %s", s["host"], type(e).__name__)
            raise DeliveryError(_value_error(e, s)) from None
        logger.info("Emailed %s (%d bytes) to %d recipient(s)", filename, len(pdf), len(to))
        return "mailto:" + ",".join(to), True

    def test(self, options: dict) -> dict:
        try:
            s = _settings(options)
            smtp = _connect(s)
        except DeliveryError as e:
            return {"ok": False, "message": str(e)}
        except (smtplib.SMTPException, OSError) as e:
            return {"ok": False, "message": _smtp_error(e, _safe_host(options))}
        except ValueError as e:
            return {"ok": False, "message": _value_error(e, _safe_host(options))}
        try:
            smtp.noop()
            smtp.quit()
        except Exception:
            pass
        return {"ok": True, "message": f"OpenScanHub can sign in to {s['host']}. No message was sent."}


def _safe_host(options: dict) -> dict:
    return {"host": str(options.get("host") or "")}
