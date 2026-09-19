"""
title: File Analyzer (Code Interpreter)
requirements: pandas,openpyxl,python-docx,pypdf,matplotlib,httpx
version: 0.3.0
"""

import base64
import contextlib
import io
import multiprocessing
import re
import sys
import tempfile
import time
import traceback
from typing import List, Optional

sys.path.insert(0, "/app/shared_lib")
try:
    from audit_logger import log_call
except ImportError:

    def log_call(*args, **kwargs):
        # audit_logger недоступен (папка /app/shared_lib не примонтирована) —
        # тул продолжает работать без логирования, просто ничего не пишет в журнал.
        pass


import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from pydantic import BaseModel, Field

try:
    import docx
except ImportError:
    docx = None
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

RESIZE_SCRIPT = """
<script>
function reportHeight() {
  var h = document.documentElement.scrollHeight;
  parent.postMessage({ type: 'iframe:height', height: h }, '*');
}
window.addEventListener('load', reportHeight);
new ResizeObserver(reportHeight).observe(document.body);
</script>
"""

# Модули, запрещённые внутри пользовательского кода в песочнице
BLOCKED_MODULES = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "shutil",
    "pathlib",
    "importlib",
    "ctypes",
    "multiprocessing",
    "threading",
    "http",
    "urllib",
    "requests",
    "httpx",
    "ftplib",
    "telnetlib",
    "pickle",
    "marshal",
    "resource",
    "signal",
}


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    top = name.split(".")[0]
    if top in BLOCKED_MODULES:
        raise ImportError(f"Импорт модуля '{name}' запрещён в песочнице file-analyzer.")
    return __import__(name, globals, locals, fromlist, level)


def _worker(
    code: str, context_vars: dict, output_path: str, queue: "multiprocessing.Queue"
):
    """Выполняется в отдельном форкнутом процессе — зависание/падение не затронет основной сервис."""
    import builtins as _builtins

    safe_builtins = {
        k: v
        for k, v in vars(_builtins).items()
        if k not in ("open", "exec", "eval", "compile", "input", "help", "breakpoint")
    }
    safe_builtins["__import__"] = _guarded_import
    # Разрешаем open() только на запись в предвычисленный output_path — иначе код не сможет
    # сохранить результат (график/файл), но не сможет читать/писать произвольные пути.
    real_open = _builtins.open

    def _restricted_open(path, mode="r", *a, **kw):
        if path != output_path or "r" in mode:
            raise PermissionError(
                "В песочнице разрешена запись только через переменную OUTPUT_PATH."
            )
        return real_open(path, mode, *a, **kw)

    safe_builtins["open"] = _restricted_open

    exec_globals = {
        "__builtins__": safe_builtins,
        "pd": pd,
        "plt": plt,
        "OUTPUT_PATH": output_path,
        **context_vars,
    }
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            exec(code, exec_globals)
        queue.put({"ok": True, "stdout": stdout.getvalue()})
    except Exception:
        queue.put(
            {"ok": False, "stdout": stdout.getvalue(), "error": traceback.format_exc()}
        )


def _decode_text_bytes(raw: bytes) -> "tuple[str, str]":
    """Декодирует байты в текст, перебирая типичные кодировки (для csv-файлов из Excel
    в русской локали обычно cp1251, реже koi8-r). latin-1 в конце никогда не падает,
    поэтому используется как последний рубеж, а не первый вариант."""
    for enc in ("utf-8-sig", "utf-8", "cp1251", "koi8-r"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return (
        raw.decode("latin-1", errors="replace"),
        "latin-1 (часть символов могла быть заменена)",
    )


def load_file_content(raw: bytes, filename: str):
    """Парсит сырые байты файла в удобные для кода переменные в зависимости от расширения."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext == "csv":
        text, encoding = _decode_text_bytes(raw)
        # sep=None + engine="python" сам определяет разделитель — в русских экспортах
        # из Excel часто ';' вместо ',', и вместе с cp1251 это самая частая связка.
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python")
        note = (
            f", кодировка определена как {encoding}"
            if encoding not in ("utf-8", "utf-8-sig")
            else ""
        )
        return {"df": df}, f"CSV, {df.shape[0]} строк x {df.shape[1]} колонок{note}"

    if ext in ("xlsx", "xls"):
        df = pd.read_excel(io.BytesIO(raw))
        return {"df": df}, f"Excel, {df.shape[0]} строк x {df.shape[1]} колонок"

    if ext == "docx":
        if docx is None:
            raise RuntimeError("Пакет python-docx не установлен.")
        d = docx.Document(io.BytesIO(raw))
        text = "\n".join(p.text for p in d.paragraphs)
        tables = []
        for t in d.tables:
            rows = [[cell.text for cell in row.cells] for row in t.rows]
            if rows:
                tables.append(pd.DataFrame(rows[1:], columns=rows[0]))
        return {
            "text": text,
            "tables": tables,
        }, f"DOCX, {len(text.split())} слов, {len(tables)} таблиц(ы)"

    if ext == "pdf":
        if PdfReader is None:
            raise RuntimeError("Пакет pypdf не установлен.")
        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return {
            "text": text
        }, f"PDF, {len(reader.pages)} страниц, {len(text.split())} слов"

    raise RuntimeError(
        f"Формат '.{ext}' не поддерживается (сейчас: csv, xlsx, xls, docx, pdf)."
    )


class Tools:
    class Valves(BaseModel):
        WEBUI_BASE_URL: str = Field(
            default="http://localhost:8080",
            description="Внутренний base URL самого Open WebUI (для скачивания/загрузки файлов через его же API)",
        )
        API_KEY: str = Field(
            default="",
            description="Опционально: API-ключ Open WebUI (Settings -> Account -> API keys). "
            "Если пусто — используется токен текущего запроса пользователя (__request__).",
            json_schema_extra={"input": {"type": "password"}},
        )
        EXEC_TIMEOUT_SECONDS: int = Field(
            default=15, description="Таймаут выполнения пользовательского кода"
        )

    def __init__(self):
        self.valves = self.Valves()

    def _auth_header(self, __request__) -> dict:
        if self.valves.API_KEY:
            return {"Authorization": f"Bearer {self.valves.API_KEY}"}
        if __request__ is not None:
            auth = __request__.headers.get("Authorization")
            if auth:
                return {"Authorization": auth}
        return {}

    async def _download_file(self, file_id: str, __request__) -> bytes:
        url = f"{self.valves.WEBUI_BASE_URL.rstrip('/')}/api/v1/files/{file_id}/content"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=self._auth_header(__request__))
            resp.raise_for_status()
            return resp.content

    async def _upload_file(self, filename: str, data: bytes, __request__) -> str:
        url = f"{self.valves.WEBUI_BASE_URL.rstrip('/')}/api/v1/files/"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers=self._auth_header(__request__),
                files={"file": (filename, data)},
            )
            resp.raise_for_status()
            return resp.json()["id"]

    def _pick_file(self, __files__: Optional[List[dict]], file_index: int) -> dict:
        if not __files__:
            raise RuntimeError("К сообщению не приложено ни одного файла.")
        if file_index >= len(__files__):
            raise RuntimeError(
                f"Файла с индексом {file_index} нет — приложено файлов: {len(__files__)}."
            )
        entry = __files__[file_index]
        file_id = entry.get("id") or entry.get("file", {}).get("id")
        filename = entry.get("name") or entry.get("file", {}).get("filename") or "file"
        if not file_id:
            raise RuntimeError("Не удалось определить id файла из __files__.")
        return {"id": file_id, "name": filename}

    async def _load_all_files(self, __files__: Optional[List[dict]], __request__):
        """Скачивает и парсит КАЖДЫЙ приложенный к сообщению файл. Возвращает
        (files_by_name, errors_by_name) — один плохой файл не должен ронять остальные.
        """
        if not __files__:
            raise RuntimeError("К сообщению не приложено ни одного файла.")

        files_by_name: dict = {}
        errors_by_name: dict = {}
        for idx in range(len(__files__)):
            info = self._pick_file(__files__, idx)
            try:
                raw = await self._download_file(info["id"], __request__)
                context, summary = load_file_content(raw, info["name"])
                files_by_name[info["name"]] = {
                    **context,
                    "_summary": summary,
                    "_size": len(raw),
                }
            except Exception as e:
                errors_by_name[info["name"]] = str(e)
        return files_by_name, errors_by_name

    def _describe_one(self, name: str, context: dict) -> str:
        lines = [f"Файл: {name}", context.get("_summary", "")]
        if "df" in context:
            df = context["df"]
            lines.append(f"Колонки: {list(df.columns)}")
            lines.append(f"Типы данных:\n{df.dtypes.to_string()}")
            lines.append(f"Первые строки:\n{df.head(5).to_string()}")
        if "text" in context:
            lines.append(f"Начало текста:\n{context['text'][:800]}")
        if context.get("tables"):
            lines.append(f"Найдено таблиц в документе: {len(context['tables'])}")
            lines.append(f"Первая таблица:\n{context['tables'][0].head(5).to_string()}")
        return "\n".join(lines)

    async def inspect_uploaded_file(
        self,
        file_index: Optional[int] = None,
        __files__: Optional[List[dict]] = None,
        __request__=None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Показывает краткую информацию о файле(ах), приложенных к сообщению (тип, размер,
        структура, превью данных) — ВСЕГДА вызывай этот тул ПЕРВЫМ, прежде чем писать код для
        run_python_on_file, чтобы не угадывать имена колонок и структуру. Если file_index не
        указан — показывает ВСЕ приложенные файлы сразу (это нужно, когда пользователь просит
        сравнить/сопоставить два и более файла, например "за 2025" и "за 2026 год").

        :param file_index: индекс конкретного файла, если нужен только один (0 — первый);
            если не передан — показываются все приложенные файлы
        """
        start = time.monotonic()
        params = {"file_index": file_index}
        try:
            files_by_name, errors_by_name = await self._load_all_files(
                __files__, __request__
            )
        except Exception as e:
            log_call(
                "File Analyzer",
                "inspect_uploaded_file",
                __user__,
                params,
                "error",
                int((time.monotonic() - start) * 1000),
                error=str(e),
            )
            return f"Ошибка чтения файла: {e}"

        names = list(files_by_name.keys())
        if file_index is not None:
            if file_index >= len(names) + len(errors_by_name):
                log_call(
                    "File Analyzer",
                    "inspect_uploaded_file",
                    __user__,
                    params,
                    "error",
                    int((time.monotonic() - start) * 1000),
                    error="bad file_index",
                )
                return f"Файла с индексом {file_index} нет."
            if file_index < len(names):
                result = self._describe_one(
                    names[file_index], files_by_name[names[file_index]]
                )
            else:
                result = f"Файл с индексом {file_index} не удалось прочитать."
        else:
            blocks = [
                self._describe_one(name, ctx) for name, ctx in files_by_name.items()
            ]
            for name, err in errors_by_name.items():
                blocks.append(f"Файл: {name}\nОшибка чтения: {err}")
            header = f"Приложено файлов: {len(names) + len(errors_by_name)}"
            result = header + "\n\n" + "\n\n---\n\n".join(blocks)

        log_call(
            "File Analyzer",
            "inspect_uploaded_file",
            __user__,
            params,
            "ok",
            int((time.monotonic() - start) * 1000),
        )
        return result

    async def run_python_on_file(
        self,
        code: str,
        file_index: int = 0,
        output_filename: Optional[str] = None,
        __files__: Optional[List[dict]] = None,
        __request__=None,
        __event_emitter__=None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Выполняет Python-код над содержимым файла(ов), приложенных к сообщению (csv, xlsx/xls,
        docx, pdf), в изолированном процессе с таймаутом. ВСЕГДА сначала вызови
        inspect_uploaded_file, чтобы узнать структуру файлов, прежде чем писать код.

        В коде НЕТ доступа к файловой системе — файлы не лежат на диске ни под каким именем.
        НЕЛЬЗЯ писать pd.read_excel("имя_файла.xlsx") или open("путь") — такого пути не существует
        и код упадёт с FileNotFoundError. Вместо этого используй уже подготовленные переменные:

          - files: dict — словарь ВСЕХ приложенных файлов, ключ — точное имя файла, значение —
            {"df": DataFrame} для csv/xlsx/xls, или {"text": str, "tables": [...]} для pdf/docx.
            Нужен, когда файлов несколько и их нужно сравнить/сопоставить — например:
              df26 = files["Таблица о наличии пожсигнализации в ПЗ 2026г.xlsx"]["df"]
              df25 = files["Таблица о наличии огнетушителей в ПЗ 2025г.xlsx"]["df"]
          - df / text / tables — то же самое, но напрямую для ОДНОГО файла с индексом file_index
            (удобно, когда файл всего один и сравнивать не с чем).
          - OUTPUT_PATH: str — путь, куда нужно сохранить результат, ЕСЛИ хочешь вернуть файл
            пользователю (например: df.to_excel(OUTPUT_PATH) или plt.savefig(OUTPUT_PATH)).
            Работает только если передан параметр output_filename.

        Если нужен только текстовый анализ (статистика, выводы) — просто используй print(...),
        вывод будет возвращён как результат. Импорт os/subprocess/socket и т.п. запрещён.

        :param code: Python-код для выполнения над файлом(ами)
        :param file_index: индекс файла для плоских переменных df/text/tables (0 — первый);
            на словарь files это не влияет — там всегда доступны все приложенные файлы
        :param output_filename: имя файла для сохранения результата (например "result.xlsx",
            "chart.png") — если код должен вернуть обработанный файл или график, а не только текст
        """
        start = time.monotonic()
        params = {
            "file_index": file_index,
            "output_filename": output_filename,
            "code": code,
        }

        def _log(status: str, error: Optional[str] = None):
            log_call(
                "File Analyzer",
                "run_python_on_file",
                __user__,
                params,
                status,
                int((time.monotonic() - start) * 1000),
                error=error,
            )

        try:
            files_by_name, errors_by_name = await self._load_all_files(
                __files__, __request__
            )
        except Exception as e:
            _log("error", str(e))
            return f"Ошибка чтения файла: {e}"

        if errors_by_name and not files_by_name:
            err_text = "; ".join(f"{n}: {e}" for n, e in errors_by_name.items())
            _log("error", err_text)
            return f"Ни один из приложенных файлов не удалось прочитать: {err_text}"

        names = list(files_by_name.keys())
        context_vars = {
            "files": {
                n: {k: v for k, v in c.items() if not k.startswith("_")}
                for n, c in files_by_name.items()
            }
        }
        if file_index < len(names):
            flat = files_by_name[names[file_index]]
            context_vars.update(
                {k: v for k, v in flat.items() if not k.startswith("_")}
            )

        tmpdir = tempfile.mkdtemp(prefix="file_analyzer_")
        output_path = (
            f"{tmpdir}/{output_filename}" if output_filename else f"{tmpdir}/__unused__"
        )

        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        proc = ctx.Process(
            target=_worker, args=(code, context_vars, output_path, queue)
        )
        proc.start()
        proc.join(self.valves.EXEC_TIMEOUT_SECONDS)

        if proc.is_alive():
            proc.terminate()
            proc.join()
            _log("error", f"timeout after {self.valves.EXEC_TIMEOUT_SECONDS}s")
            return f"Код выполнялся дольше {self.valves.EXEC_TIMEOUT_SECONDS} сек и был прерван."

        if queue.empty():
            _log("error", "worker process crashed without a result")
            return "Процесс завершился аварийно без результата (возможно, вышел за лимит памяти)."

        result = queue.get()
        if not result["ok"]:
            _log("error", result["error"])
            return f"Ошибка выполнения кода:\n{result['error']}\n\nВывод до ошибки:\n{result['stdout']}"

        output_text = result["stdout"].strip() or "(код выполнен без вывода в print)"

        import os as _os_host  # используем os тут, на хосте tool-процесса — это не sandboxed-код

        if output_filename and _os_host.path.exists(output_path):
            with open(output_path, "rb") as f:
                out_bytes = f.read()

            if output_filename.lower().endswith((".png", ".jpg", ".jpeg")):
                b64 = base64.b64encode(out_bytes).decode()
                html = (
                    '<div style="font-family:system-ui;padding:0.5rem;text-align:center;">'
                    f'<img src="data:image/png;base64,{b64}" style="max-width:100%;height:auto;" />'
                    "</div>" + RESIZE_SCRIPT
                )
                if __event_emitter__:
                    await __event_emitter__(
                        {"type": "embeds", "data": {"embeds": [html], "replace": False}}
                    )
                _log("ok")
                return f"{output_text}\n\nГрафик построен и вставлен в чат выше."

            try:
                new_id = await self._upload_file(
                    output_filename, out_bytes, __request__
                )
                if __event_emitter__:
                    file_url = f"{self.valves.WEBUI_BASE_URL.rstrip('/')}/api/v1/files/{new_id}/content"
                    await __event_emitter__(
                        {
                            "type": "files",
                            "data": {
                                "files": [
                                    {
                                        "type": "file",
                                        "url": file_url,
                                        "name": output_filename,
                                    }
                                ]
                            },
                        }
                    )
                _log("ok")
                return f"{output_text}\n\nФайл '{output_filename}' готов и прикреплён к сообщению выше."
            except Exception as e:
                _log("error", f"upload failed: {e}")
                return f"{output_text}\n\nКод выполнился успешно, но не удалось прикрепить файл: {e}"

        _log("ok")
        return output_text
