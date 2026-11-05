"""Private Telegram bot: Microsoft Windows 11 ISO -> SHA256 -> Gofile link."""
import hashlib
import html
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser

PAGE = "https://www.microsoft.com/en-us/software-download/windows11"
API = "https://www.microsoft.com/software-download-connector/api/"
MIN_ISO_BYTES = 1024**3
MAX_ISO_BYTES = 12 * 1024**3


class Problem(Exception):
    pass


def official_url(url):
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if (p.scheme != "https" or not (host == "microsoft.com" or
            host.endswith(".microsoft.com")) or p.username or p.password or
            p.port not in (None, 443)):
        raise Problem("Разрешены только HTTPS-ссылки на серверы microsoft.com.")
    return url


class MicrosoftRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Editions(HTMLParser):
    def __init__(self):
        super().__init__()
        self.value = None
        self.text = ""
        self.options = []

    def handle_starttag(self, tag, attrs):
        if tag == "option":
            self.value = dict(attrs).get("value")
            self.text = ""

    def handle_data(self, data):
        if self.value is not None:
            self.text += data

    def handle_endtag(self, tag):
        if tag == "option":
            if self.value and self.value.isdigit():
                self.options.append((self.value, self.text))
            self.value = None


def page_details(source):
    parser = Editions()
    parser.feed(source)
    choices = [v for v, label in parser.options
               if "Windows 11" in label and "x64" in label.lower()]
    if len(set(choices)) != 1:
        raise Problem("Microsoft изменил список выпусков. Нужны ISO_URL и ISO_SHA256.")
    plain = html.unescape(re.sub(r"<[^>]+>", " ", source))
    match = re.search(r"Russian\s+64[\s-]*bit\s+([A-Fa-f0-9]{64})\b", plain)
    if not match:
        raise Problem("Microsoft не опубликовал распознаваемый SHA-256 русского ISO. "
                      "Укажите ISO_URL и ISO_SHA256 из официальной страницы.")
    return choices[0], match.group(1).lower()


def microsoft_opener():
    opener = urllib.request.build_opener(
        MicrosoftRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 WindowsIsoPersonalBot/1.0"),
                         ("Accept-Encoding", "identity")]
    return opener


def resolve_iso(opener):
    override = os.getenv("ISO_URL", "").strip()
    expected = os.getenv("ISO_SHA256", "").strip().lower()
    if override:
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise Problem("Для ISO_URL задайте ISO_SHA256: 64 символа из таблицы Microsoft.")
        return official_url(override), expected, "Windows 11 Russian x64 (ISO_URL)"
    with opener.open(PAGE, timeout=60) as r:
        source = r.read(4 * 1024**2).decode("utf-8", errors="replace")
    edition, expected = page_details(source)
    session = str(uuid.uuid4())

    def call(action, **extra):
        query = {"profile": "606624d44113", "Locale": "en-US", "sessionID": session,
                 "ProductEditionId": "undefined", "SKU": "undefined",
                 "friendlyFileName": "undefined", **extra}
        with opener.open(API + action + "?" + urllib.parse.urlencode(query), timeout=60) as r:
            data = json.load(r)
        if data.get("Errors") or data.get("ValidationContainer", {}).get("Errors"):
            raise Problem("Microsoft отклонил запрос с сервера БотХост. "
                          "Получите свежую официальную ссылку в другой сети и задайте "
                          "ISO_URL вместе с ISO_SHA256. Зеркала не используются.")
        return data

    skus = call("getskuinformationbyproductedition", ProductEditionId=edition).get("Skus", [])
    ru = [s for s in skus if s.get("Language") == "Russian"]
    if len(ru) != 1:
        raise Problem("Русский выпуск не найден в каталоге Microsoft.")
    links = call("GetProductDownloadLinksBySku", SKU=ru[0]["Id"])
    choices = [x for x in links.get("ProductDownloadOptions", [])
               if str(x.get("DownloadType")) == "64" or
               "x64" in str(x.get("Uri", "")).lower()]
    if len(choices) != 1:
        raise Problem("Microsoft не выдал единственную ссылку x64. Задайте ISO_URL и ISO_SHA256.")
    return official_url(choices[0]["Uri"]), expected, ru[0].get("ProductDisplayName", "Windows 11")


def save_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def file_hashes(path, check=lambda: None):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as f:
        while block := f.read(1024**2):
            check()
            sha.update(block)
            md5.update(block)
    return sha.hexdigest(), md5.hexdigest()


def download_iso(opener, url, expected, target, check, progress):
    partial = target.with_suffix(".partial")
    partial.unlink(missing_ok=True)
    try:
        with opener.open(official_url(url), timeout=60) as r:
            official_url(r.geturl())
            length = int(r.headers.get("Content-Length", "0"))
            if length and not MIN_ISO_BYTES <= length <= MAX_ISO_BYTES:
                raise Problem("Ответ Microsoft имеет неожиданный размер вместо ISO.")
            required = (length or MAX_ISO_BYTES) + 512 * 1024**2
            if shutil.disk_usage(target.parent).free < required:
                raise Problem(f"Недостаточно места на хостинге: нужно {required / 1024**3:.1f} ГиБ свободно.")
            sha, md5 = hashlib.sha256(), hashlib.md5()
            total = 0
            with partial.open("wb") as f:
                while block := r.read(1024**2):
                    check()
                    total += len(block)
                    if total > MAX_ISO_BYTES:
                        raise Problem("Превышен предел ISO 12 ГиБ.")
                    f.write(block)
                    sha.update(block)
                    md5.update(block)
                    progress(f"Скачано {total / 1024**3:.2f} ГиБ" +
                             (f" из {length / 1024**3:.2f}" if length else ""))
            if total < MIN_ISO_BYTES or (length and total != length):
                raise Problem("ISO скачан не полностью.")
            if sha.hexdigest() != expected.lower():
                raise Problem("SHA-256 не совпал с Microsoft. Файл не будет загружен на Диск.")
            with partial.open("rb") as f:
                f.seek(32769)
                if f.read(5) != b"CD001":
                    raise Problem("В файле не найдена структура ISO9660.")
        partial.replace(target)
        return {"sha256": sha.hexdigest(), "md5": md5.hexdigest(), "size": total}
    finally:
        partial.unlink(missing_ok=True)




class Bot:
    def __init__(self):
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("Задайте BOT_TOKEN в переменных БотХост.")
        self.base = "https://api.telegram.org/bot" + token + "/"
        self.owner = int(os.getenv("OWNER_TELEGRAM_ID", "0"))
        self.root = Path(os.getenv("DATA_DIR", "/app/data/windows-iso")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.status = "Ожидание /download"
        self.stop = threading.Event()
        self.job = None

    def api(self, method, **params):
        payload = json.dumps(params).encode()
        for attempt in range(5):
            req = urllib.request.Request(self.base + method, data=payload,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=70) as r:
                    data = json.load(r)
                if not data.get("ok"):
                    raise Problem("Ошибка Telegram API.")
                return data["result"]
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry = json.load(exc).get("parameters", {}).get("retry_after", 5)
                    time.sleep(min(int(retry), 120))
                elif exc.code >= 500:
                    time.sleep(2**attempt)
                else:
                    raise Problem(f"Telegram API: HTTP {exc.code}.") from None
        raise Problem("Telegram API временно недоступен.")

    def say(self, text):
        self.api("sendMessage", chat_id=self.owner, text=text, disable_web_page_preview=True)

    def check(self):
        if self.stop.is_set():
            raise Problem("Операция отменена. Проверенный ISO, если уже скачан, сохранён в кэше.")

    def work(self):
        try:
            target = self.root / "Windows11_Russian_x64.iso"
            manifest = self.root / "verified.json"
            info = None
            if target.exists() and manifest.exists():
                self.status = "Повторная проверка ISO в кэше"
                cached = json.loads(manifest.read_text(encoding="utf-8"))
                sha, md5 = file_hashes(target, self.check)
                override_sha = os.getenv("ISO_SHA256", "").strip().lower()
                if sha == cached.get("sha256") and (not override_sha or sha == override_sha):
                    info = {**cached, "md5": md5, "size": target.stat().st_size}
            if info is None:
                # Old, unverified cache must not consume space needed for a fresh ISO.
                target.unlink(missing_ok=True)
                self.status = "Получение ссылки Microsoft"
                opener = microsoft_opener()
                url, sha, release = resolve_iso(opener)
                self.say("Источник: Microsoft. " + release + ". Начинаю скачивание и проверку SHA-256.")
                info = download_iso(opener, url, sha, target, self.check,
                                    lambda s: setattr(self, "status", s))
                info["release"] = release
                save_json(manifest, info)
            self.check()
            self.status = "Загрузка проверенного ISO на Gofile"
            self.say("SHA-256 подтверждён. Загружаю ISO целиком на gofile.io.")
            from gofile_upload import publish, UploadProblem
            try:
                result = publish(target, info, self.root, self.check,
                                 lambda s: setattr(self, "status", s))
            except UploadProblem as exc:
                raise Problem(str(exc)) from None
            self.status = "Готово. " + result["url"]
            self.say("Готово: " + result["url"] +
                     "\nISO одним файлом, без разбиения.\nSHA-256: " + info["sha256"] +
                     "\nРазмер: " + str(info["size"]) + " байт.\n"
                     "Ссылка доступна любому, у кого она есть. Срок хранения Gofile не гарантирован.\n"
                     "Скачайте ISO на внутренний диск и сравните Get-FileHash -Algorithm SHA256.")

        except Exception as exc:
            # Never print exception URLs: Telegram tokens and signed ISO URLs are secrets.
            detail = str(exc) if isinstance(exc, Problem) else self.error_text(exc)
            self.status = "Остановлено: " + detail
            try:
                self.say(self.status)
            except Exception:
                print("Не удалось отправить сообщение об ошибке.", flush=True)

    @staticmethod
    def error_text(exc):
        if isinstance(exc, urllib.error.HTTPError):
            return f"Сервер загрузки ответил HTTP {exc.code}; ссылка могла истечь или доступ ограничен."
        if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
            return "Сетевой сбой при обращении к серверу. Попробуйте /download позже."
        code = getattr(getattr(exc, "resp", None), "status", None)
        if code:
            return f"API загрузки: HTTP {code}."
        return "Ошибка " + type(exc).__name__ + ". Проверьте конфигурацию и доступ к сети."

    def run(self):
        self.api("deleteWebhook", drop_pending_updates=False)
        offset = 0
        started = time.time() - 2
        print("Бот запущен. Токены в лог не выводятся.", flush=True)
        while True:
            try:
                updates = self.api("getUpdates", offset=offset, timeout=50,
                                   allowed_updates=["message"])
                for update in updates:
                    offset = update["update_id"] + 1
                    m = update.get("message", {})
                    if m.get("date", 0) < started or m.get("chat", {}).get("type") != "private":
                        continue
                    uid = m.get("from", {}).get("id")
                    command = m.get("text", "").split(maxsplit=1)[0].split("@")[0] if m.get("text") else ""
                    if command == "/myid":
                        self.api("sendMessage", chat_id=m["chat"]["id"], text=f"Ваш Telegram ID: {uid}")
                        continue
                    if not self.owner or uid != self.owner:
                        continue
                    if command == "/status":
                        self.say(self.status)
                    elif command == "/link":
                        saved = self.root / "gofile-result.json"
                        if saved.exists():
                            item = json.loads(saved.read_text(encoding="utf-8"))
                            self.say("Последняя ссылка: " + item["url"] + "\nSHA-256: " + item["sha256"] +
                                     "\nДоступность старой ссылки не проверена. Если файл удалён, отправьте /download.")
                        else:
                            self.say("Готовой ссылки пока нет. Отправьте /download.")
                    elif command == "/cancel":
                        self.stop.set()
                        self.say("Отмена запрошена; ожидаю завершения текущего сетевого запроса.")
                    elif command == "/download":
                        if self.job and self.job.is_alive():
                            self.say("Операция уже выполняется. " + self.status)
                            continue
                        self.stop.clear()
                        self.job = threading.Thread(target=self.work, daemon=True)
                        self.job.start()
                    else:
                        self.say("/download — Microsoft ISO → проверка → Gofile\n"
                                 "/status — прогресс\n/link — последняя ссылка\n/cancel — остановить\n/myid — ваш Telegram ID")
            except Exception as exc:
                print("Сбой опроса Telegram: " + type(exc).__name__, flush=True)
                time.sleep(5)


if __name__ == "__main__":
    Bot().run()
