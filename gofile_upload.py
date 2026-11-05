"""Gofile uploader using bounded memory and public download-page links."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

HOSTS = {"upload.gofile.io", "upload-eu-par.gofile.io", "upload-na-phx.gofile.io",
         "upload-ap-sgp.gofile.io", "upload-ap-hkg.gofile.io",
         "upload-ap-tyo.gofile.io", "upload-sa-sao.gofile.io"}


class UploadProblem(Exception):
    pass


def checked_link(value):
    p = urlsplit(value)
    if (p.scheme != "https" or p.hostname != "gofile.io" or p.username or p.password or
            p.port not in (None, 443) or p.query or p.fragment or
            not re.fullmatch(r"/d/[A-Za-z0-9_-]+/?", p.path)):
        raise UploadProblem("Gofile вернул неожиданный адрес скачивания.")
    return value


def atomic_json(path, value, secret=False):
    temp = path.with_suffix(".new")
    # Secrets are created with mode 0600 on Linux, before any contents are written.
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if secret else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    temp.replace(path)
    if secret:
        os.chmod(path, 0o600)


def parse_response(status, body):
    if not 200 <= status < 300:
        raise UploadProblem(f"Gofile: HTTP {status}. Проверьте доступ к сервису/регион загрузки.")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise UploadProblem("Gofile ответил не JSON; возможно, сервис временно недоступен.") from None
    if not isinstance(data, dict) or data.get("status") != "ok":
        # Do not include raw response: it can contain account tokens.
        raise UploadProblem("Gofile отклонил запрос. Проверьте токен, квоту и доступность API.")
    if not isinstance(data.get("data"), dict):
        raise UploadProblem("Неожиданный формат ответа Gofile.")
    return data["data"]


def account_token(root, check):
    supplied = os.getenv("GOFILE_TOKEN", "").strip()
    if supplied:
        return supplied
    saved = Path(root) / "gofile-account.json"
    if saved.exists():
        token = json.loads(saved.read_text(encoding="utf-8")).get("token")
        if isinstance(token, str) and token:
            return token
        raise UploadProblem("Повреждён gofile-account.json; задайте GOFILE_TOKEN.")
    check()
    conn = http.client.HTTPSConnection("api.gofile.io", timeout=60)
    try:
        conn.request("POST", "/accounts", body=b"{}", headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = parse_response(response.status, response.read(1024**2))
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise UploadProblem("Gofile не выдал гостевой токен. Задайте GOFILE_TOKEN из своего профиля.")
        atomic_json(saved, {"token": token}, secret=True)
        return token
    except (OSError, http.client.HTTPException):
        raise UploadProblem("Не удалось связаться с API аккаунтов Gofile. Повторите позже.") from None
    finally:
        conn.close()


def upload(path, info, token, check, progress):
    host = os.getenv("GOFILE_UPLOAD_HOST", "upload.gofile.io").strip()
    if host not in HOSTS:
        raise UploadProblem("GOFILE_UPLOAD_HOST не входит в список официальных серверов загрузки.")
    path = Path(path)
    name = path.name
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise UploadProblem("Имя ISO должно содержать только ASCII-буквы, цифры, точку, _ и -.")
    for attempt in range(3):
        check()
        boundary = "gofileiso" + uuid.uuid4().hex
        prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                  f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode("ascii")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        conn = http.client.HTTPSConnection(host, timeout=120)
        try:
            conn.putrequest("POST", "/uploadfile")
            conn.putheader("Authorization", "Bearer " + token)
            conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            conn.putheader("Content-Length", str(len(prefix) + info["size"] + len(suffix)))
            conn.putheader("User-Agent", "WindowsIsoPersonalBot/1.0")
            conn.endheaders(prefix)
            total = 0
            sha, md5 = hashlib.sha256(), hashlib.md5()
            with path.open("rb") as f:
                while total < info["size"]:
                    check()
                    block = f.read(min(1024**2, info["size"] - total))
                    if not block:
                        raise UploadProblem("ISO был изменён во время отправки: файл стал короче.")
                    conn.send(block)
                    total += len(block)
                    sha.update(block)
                    md5.update(block)
                    progress(f"Gofile: {total / info['size'] * 100:.1f}% "
                             f"({total / 1024**3:.2f} ГиБ)")
                if f.read(1):
                    raise UploadProblem("ISO был изменён во время отправки: файл стал длиннее.")
            if sha.hexdigest() != info["sha256"] or md5.hexdigest() != info["md5"]:
                raise UploadProblem("ISO изменился после проверки; отправка прервана.")
            conn.send(suffix)
            response = conn.getresponse()
            body = response.read(1024**2)
            if response.status == 429 or response.status >= 500:
                if attempt == 2:
                    raise UploadProblem(f"Gofile временно недоступен: HTTP {response.status}.")
                retry = response.getheader("Retry-After", "10")
                time.sleep(min(int(retry) if retry.isdigit() else 10, 60))
                continue
            data = parse_response(response.status, body)
            if (str(data.get("md5", "")).lower() != info["md5"] or
                    int(data.get("size", -1)) != info["size"]):
                raise UploadProblem("Размер или MD5 в ответе Gofile не совпал. Загрузка не подтверждена.")
            return {"url": checked_link(data.get("downloadPage", "")),
                    "id": data.get("id") or data.get("fileId"),
                    "parentFolder": data.get("parentFolder"), "size": info["size"],
                    "sha256": info["sha256"], "md5": info["md5"],
                    "uploaded_at": int(time.time())}
        except (OSError, http.client.HTTPException):
            if attempt == 2:
                raise UploadProblem("Сетевой сбой Gofile. ISO остаётся в кэше, повторите /download.") from None
            time.sleep(2 ** (attempt + 1))
        finally:
            conn.close()
    raise UploadProblem("Не удалось загрузить ISO на Gofile.")


def publish(path, info, root, check, progress):
    token = account_token(root, check)
    result = upload(path, info, token, check, progress)
    atomic_json(Path(root) / "gofile-result.json", result)
    return result
