"""A small IMAP4rev1 server for tests. It speaks the real wire protocol over
a socket (optionally TLS or STARTTLS), so imaplib talks to it as to any
server. It implements what OpenScanHub uses: CAPABILITY, STARTTLS, LOGIN,
SELECT, LIST, CREATE, UID SEARCH UNSEEN, UID FETCH, UID STORE, UID COPY,
EXPUNGE, UID EXPUNGE (with uidplus=True), LOGOUT."""

import re
import socket
import ssl
import threading
from typing import Optional


class FakeImapServer:
    def __init__(self, user="scan", password="mail-pw-123", ssl_context: Optional[ssl.SSLContext] = None,
                 implicit_tls=False, uidplus=False):
        self.user, self.password = user, password
        self.uidplus = uidplus
        self.ssl_context = ssl_context
        self.implicit_tls = implicit_tls
        self.folders: dict[str, list[dict]] = {"INBOX": []}  # name -> messages
        self._next_uid = 1
        self.log: list[str] = []  # commands received, passwords blanked
        self.lock = threading.RLock()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def add(self, raw: bytes, folder="INBOX", seen=False, deleted=False) -> int:
        with self.lock:
            uid = self._next_uid
            self._next_uid += 1
            flags = {"\\Seen"} if seen else set()
            if deleted:
                flags.add("\\Deleted")
            self.folders.setdefault(folder, []).append({"uid": uid, "raw": raw, "flags": flags})
            return uid

    def messages(self, folder):
        with self.lock:
            return list(self.folders.get(folder, []))

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            if self.implicit_tls:
                conn = self.ssl_context.wrap_socket(conn, server_side=True)
            f = conn.makefile("rwb", buffering=0)
            f.write(b"* OK fake IMAP ready\r\n")
            state = {"auth": False, "folder": None, "conn": conn, "f": f}
            while True:
                line = state["f"].readline()
                if not line:
                    return
                if not self._command(state, line.decode().rstrip("\r\n")):
                    return
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, state, text: str):
        state["f"].write(text.encode() + b"\r\n")

    def _command(self, st, line: str) -> bool:
        tag, _, rest = line.partition(" ")
        cmd, _, args = rest.partition(" ")
        cmd = cmd.upper()
        self.log.append(f"{cmd} ***" if cmd == "LOGIN" else rest)
        ok = lambda msg="done": self._send(st, f"{tag} OK {msg}")
        no = lambda msg="failed": self._send(st, f"{tag} NO {msg}")
        if cmd == "CAPABILITY":
            caps = "IMAP4rev1" + (" STARTTLS" if self.ssl_context and not self.implicit_tls else "")
            caps += " UIDPLUS" if self.uidplus else ""
            self._send(st, f"* CAPABILITY {caps}")
            ok()
        elif cmd == "STARTTLS":
            self._send(st, f"{tag} OK begin TLS")
            raw = self.ssl_context.wrap_socket(st["conn"], server_side=True)
            st["conn"] = raw
            st["f"] = raw.makefile("rwb", buffering=0)
        elif cmd == "LOGIN":
            m = re.match(r'"?([^"\s]*)"?\s+"?(.*?)"?$', args)
            if m and m.group(1) == self.user and m.group(2) == self.password:
                st["auth"] = True
                ok("logged in")
            else:
                no("authentication failed")
        elif not st["auth"]:
            no("log in first")
        elif cmd == "SELECT":
            name = args.strip('"')
            if name not in self.folders:
                no("no such folder")
            else:
                st["folder"] = name
                self._send(st, f"* {len(self.folders[name])} EXISTS")
                ok("[READ-WRITE] selected")
        elif cmd == "LIST":
            m = re.match(r'"[^"]*"\s+"?(.*?)"?$', args)
            name = m.group(1) if m else ""
            if name in self.folders:
                self._send(st, f'* LIST (\\HasNoChildren) "/" "{name}"')
            ok()
        elif cmd == "CREATE":
            self.folders.setdefault(args.strip('"'), [])
            ok()
        elif cmd == "UID":
            self._uid(st, tag, args)
        elif cmd == "EXPUNGE":
            with self.lock:
                msgs = self.folders[st["folder"]]
                self.folders[st["folder"]] = [m for m in msgs if "\\Deleted" not in m["flags"]]
            ok()
        elif cmd == "LOGOUT":
            self._send(st, "* BYE bye")
            ok()
            return False
        else:
            no("unknown command")
        return True

    def _uid(self, st, tag, args):
        sub, _, rest = args.partition(" ")
        sub = sub.upper()
        msgs = self.folders[st["folder"]]
        if sub == "SEARCH":
            uids = " ".join(str(m["uid"]) for m in msgs if "\\Seen" not in m["flags"])
            self._send(st, "* SEARCH " + uids if uids else "* SEARCH")
            self._send(st, f"{tag} OK done")
            return
        uid_s, _, tail = rest.partition(" ")
        if sub == "EXPUNGE" and self.uidplus:
            with self.lock:
                self.folders[st["folder"]] = [m for m in msgs if not (
                    str(m["uid"]) == uid_s and "\\Deleted" in m["flags"])]
            self._send(st, f"{tag} OK done")
            return
        target = [m for m in msgs if str(m["uid"]) == uid_s]
        if not target:
            self._send(st, f"{tag} OK nothing")
            return
        m = target[0]
        if sub == "FETCH" and "RFC822.SIZE" in tail.upper():
            self._send(st, f"* 1 FETCH (UID {m['uid']} RFC822.SIZE {len(m['raw'])})")
            self._send(st, f"{tag} OK done")
        elif sub == "FETCH":
            raw = m["raw"]
            f = st["f"]
            f.write(f"* 1 FETCH (UID {m['uid']} BODY[] {{{len(raw)}}}\r\n".encode() + raw + b")\r\n")
            self._send(st, f"{tag} OK done")
        elif sub == "STORE":
            flag = re.search(r"\(([^)]*)\)", tail).group(1)
            if tail.startswith("+FLAGS"):
                m["flags"].add(flag)
            self._send(st, f"{tag} OK done")
        elif sub == "COPY":
            dest = tail.strip('"')
            if dest not in self.folders:
                self._send(st, f"{tag} NO [TRYCREATE] no such folder")
                return
            self.add(m["raw"], folder=dest, seen=True)
            self._send(st, f"{tag} OK done")
        else:
            self._send(st, f"{tag} NO unknown")
