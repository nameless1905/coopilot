"""
title: QC Chart Builder
requirements: psycopg2-binary,sqlglot,pandas,matplotlib
version: 0.5.0
"""

import base64
import io
from typing import Literal, Optional, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import psycopg2
import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Alter,
    exp.Create,
)

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


def is_safe_select(sql: str) -> tuple[bool, str]:
    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except Exception as e:
        return False, f"Не удалось разобрать SQL: {e}"
    if len(statements) != 1:
        return False, "Разрешён ровно один SQL-оператор за запрос."
    stmt = statements[0]
    if stmt is None or not isinstance(stmt, exp.Select):
        return (
            False,
            "Верхний уровень запроса должен быть SELECT (включая WITH ... SELECT).",
        )
    if list(stmt.find_all(*FORBIDDEN)):
        return False, "Обнаружена операция изменения данных внутри запроса — запрещено."
    return True, ""


def render_chart_block(dsn, sql, chart_type, x_col, y_col, title, row_limit) -> str:
    """Строит один график и возвращает готовый HTML-фрагмент (картинку или сообщение об ошибке)."""
    ok, reason = is_safe_select(sql)
    if not ok:
        return f'<div style="padding:0.5rem;color:#c00;">Запрос отклонён ({title or sql[:40]}): {reason}</div>'

    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '5000'")
        cur.execute(f"SELECT * FROM ({sql.rstrip(';')}) AS _sub LIMIT {row_limit}")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        return f'<div style="padding:0.5rem;color:#c00;">Ошибка запроса ({title or sql[:40]}): {e}</div>'

    if not rows:
        return f'<div style="padding:0.5rem;color:#666;">{title or sql[:40]}: запрос вернул 0 строк</div>'

    df = pd.DataFrame(rows, columns=cols)
    x = x_col or df.columns[0]
    y = y_col or (df.columns[1] if len(df.columns) > 1 else df.columns[0])
    if x not in df.columns or y not in df.columns:
        return f"<div style=\"padding:0.5rem;color:#c00;\">Колонки '{x}'/'{y}' не найдены. Доступны: {list(df.columns)}</div>"

    try:
        fig, ax = plt.subplots(figsize=(6, 3.5), dpi=90)
        if chart_type == "bar":
            ax.bar(df[x].astype(str), df[y])
            ax.tick_params(axis="x", rotation=45)
        elif chart_type == "line":
            ax.plot(df[x].astype(str), df[y], marker="o")
            ax.tick_params(axis="x", rotation=45)
        elif chart_type == "pie":
            ax.pie(df[y], labels=df[x].astype(str), autopct="%1.1f%%")
        elif chart_type == "scatter":
            ax.scatter(df[x], df[y])

        if chart_type != "pie":
            ax.set_xlabel(x)
            ax.set_ylabel(y)
            ax.grid(True, alpha=0.3)
        if title:
            ax.set_title(title)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode()
    except Exception as e:
        plt.close("all")
        return f'<div style="padding:0.5rem;color:#c00;">Ошибка построения графика ({title}): {e}</div>'

    return (
        '<div style="font-family:system-ui;padding:0.5rem;text-align:center;border-bottom:1px solid #eee;">'
        f'<img src="data:image/png;base64,{b64}" style="max-width:100%;height:auto;" />'
        + (f'<div style="margin-top:0.5rem;color:#666;">{title}</div>' if title else "")
        + "</div>"
    )


class Tools:
    class Valves(BaseModel):
        TEST_DB_DSN: str = Field(
            default="postgresql://readonly_user:readonly_pass@test-db:5432/testdb",
            description="Read-only DSN тестовой (синтетической) БД",
            json_schema_extra={"input": {"type": "password"}},
        )
        PROD_DB_DSN: str = Field(
            default="",
            description="Read-only DSN боевой БД QC-мониторинга",
            json_schema_extra={"input": {"type": "password"}},
        )

    def __init__(self):
        self.valves = self.Valves()

    async def plot_qc_data(
        self,
        sql: str,
        chart_type: Literal["bar", "line", "pie", "scatter"] = "bar",
        x_col: Optional[str] = None,
        y_col: Optional[str] = None,
        title: str = "",
        connection: Literal["test", "prod"] = "test",
        row_limit: int = 500,
        __event_emitter__=None,
    ) -> str:
        """
        Строит ОДИН график по агрегирующему SELECT-запросу. Если нужно несколько
        графиков сразу — используй plot_qc_charts, а не несколько вызовов этого тула
        подряд (иначе они могут наложиться друг на друга в чате).

        :param sql: агрегирующий SQL-запрос (включая WITH ... SELECT)
        :param chart_type: тип графика: bar, line, pie, scatter
        :param x_col: колонка для оси X / категорий (по умолчанию — первая колонка результата)
        :param y_col: колонка для оси Y / значений (по умолчанию — вторая колонка результата)
        :param title: заголовок графика
        :param connection: к какой БД обращаться: test (синтетическая) или prod (боевая)
        :param row_limit: максимум строк, которые попадут в график
        """
        dsn = {"test": self.valves.TEST_DB_DSN, "prod": self.valves.PROD_DB_DSN}[
            connection
        ]
        if not dsn:
            return f"Подключение '{connection}' не настроено — пустой DSN в Valves."

        block = render_chart_block(dsn, sql, chart_type, x_col, y_col, title, row_limit)
        html = block + RESIZE_SCRIPT

        if __event_emitter__:
            await __event_emitter__(
                {"type": "embeds", "data": {"embeds": [html], "replace": False}}
            )
            return "График построен и вставлен в чат выше."
        return html

    async def plot_qc_charts(
        self,
        charts: List[dict],
        connection: Literal["test", "prod"] = "test",
        row_limit: int = 500,
        __event_emitter__=None,
    ) -> str:
        """
        Строит ЛЮБОЕ количество графиков (не ограничено) за один вызов и вставляет
        их все одним блоком в чат. Используй этот тул, когда пользователь просит
        построить два и более графика сразу — так они не наложатся друг на друга.

        :param charts: список графиков произвольной длины. Каждый элемент — объект вида
            {"sql": "SELECT ...", "chart_type": "bar", "title": "...", "x_col": "...", "y_col": "..."}.
            Обязательно только поле sql, остальные — опциональны (chart_type по умолчанию "bar").
            Пример: [
              {"sql": "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status", "chart_type": "bar", "title": "Заказы по статусам"},
              {"sql": "SELECT category, SUM(price) AS revenue FROM products GROUP BY category", "chart_type": "pie", "title": "Выручка по категориям"}
            ]
        :param connection: к какой БД обращаться: test или prod
        :param row_limit: лимит строк на каждый график
        """
        if not charts:
            return "Список графиков пуст."

        dsn = {"test": self.valves.TEST_DB_DSN, "prod": self.valves.PROD_DB_DSN}[
            connection
        ]
        if not dsn:
            return f"Подключение '{connection}' не настроено — пустой DSN в Valves."

        blocks = []
        for spec in charts:
            sql = (spec.get("sql") or "").strip()
            if not sql:
                continue
            blocks.append(
                render_chart_block(
                    dsn,
                    sql,
                    spec.get("chart_type", "bar"),
                    spec.get("x_col"),
                    spec.get("y_col"),
                    spec.get("title", ""),
                    row_limit,
                )
            )

        if not blocks:
            return "Ни в одном элементе списка не передан непустой SQL."

        html = "".join(blocks) + RESIZE_SCRIPT

        if __event_emitter__:
            await __event_emitter__(
                {"type": "embeds", "data": {"embeds": [html], "replace": False}}
            )
            return (
                f"Построено графиков: {len(blocks)}. Вставлены в чат одним блоком выше."
            )
        return "Ошибка: __event_emitter__ недоступен." """
title: QC Chart Builder
requirements: psycopg2-binary,sqlglot,pandas,matplotlib
version: 0.5.0
"""


import base64
import io
from typing import Literal, Optional, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import psycopg2
import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Alter,
    exp.Create,
)

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


def is_safe_select(sql: str) -> tuple[bool, str]:
    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except Exception as e:
        return False, f"Не удалось разобрать SQL: {e}"
    if len(statements) != 1:
        return False, "Разрешён ровно один SQL-оператор за запрос."
    stmt = statements[0]
    if stmt is None or not isinstance(stmt, exp.Select):
        return (
            False,
            "Верхний уровень запроса должен быть SELECT (включая WITH ... SELECT).",
        )
    if list(stmt.find_all(*FORBIDDEN)):
        return False, "Обнаружена операция изменения данных внутри запроса — запрещено."
    return True, ""


def render_chart_block(dsn, sql, chart_type, x_col, y_col, title, row_limit) -> str:
    """Строит один график и возвращает готовый HTML-фрагмент (картинку или сообщение об ошибке)."""
    ok, reason = is_safe_select(sql)
    if not ok:
        return f'<div style="padding:0.5rem;color:#c00;">Запрос отклонён ({title or sql[:40]}): {reason}</div>'

    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '5000'")
        cur.execute(f"SELECT * FROM ({sql.rstrip(';')}) AS _sub LIMIT {row_limit}")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        return f'<div style="padding:0.5rem;color:#c00;">Ошибка запроса ({title or sql[:40]}): {e}</div>'

    if not rows:
        return f'<div style="padding:0.5rem;color:#666;">{title or sql[:40]}: запрос вернул 0 строк</div>'

    df = pd.DataFrame(rows, columns=cols)
    x = x_col or df.columns[0]
    y = y_col or (df.columns[1] if len(df.columns) > 1 else df.columns[0])
    if x not in df.columns or y not in df.columns:
        return f"<div style=\"padding:0.5rem;color:#c00;\">Колонки '{x}'/'{y}' не найдены. Доступны: {list(df.columns)}</div>"

    # psycopg2 отдаёт NUMERIC/DECIMAL-колонки как Python Decimal, а не float —
    # matplotlib/numpy не умеют с ними работать напрямую, поэтому приводим явно.
    df[y] = pd.to_numeric(df[y], errors="coerce")
    if df[y].isna().all():
        return f"<div style=\"padding:0.5rem;color:#c00;\">Колонка '{y}' не числовая — график по ней невозможен.</div>"

    try:
        fig, ax = plt.subplots(figsize=(6, 3.5), dpi=90)
        if chart_type == "bar":
            ax.bar(df[x].astype(str), df[y])
            ax.tick_params(axis="x", rotation=45)
        elif chart_type == "line":
            ax.plot(df[x].astype(str), df[y], marker="o")
            ax.tick_params(axis="x", rotation=45)
        elif chart_type == "pie":
            ax.pie(df[y], labels=df[x].astype(str), autopct="%1.1f%%")
        elif chart_type == "scatter":
            ax.scatter(df[x], df[y])

        if chart_type != "pie":
            ax.set_xlabel(x)
            ax.set_ylabel(y)
            ax.grid(True, alpha=0.3)
        if title:
            ax.set_title(title)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode()
    except Exception as e:
        plt.close("all")
        return f'<div style="padding:0.5rem;color:#c00;">Ошибка построения графика ({title}): {e}</div>'

    return (
        '<div style="font-family:system-ui;padding:0.5rem;text-align:center;border-bottom:1px solid #eee;">'
        f'<img src="data:image/png;base64,{b64}" style="max-width:100%;height:auto;" />'
        + (f'<div style="margin-top:0.5rem;color:#666;">{title}</div>' if title else "")
        + "</div>"
    )


class Tools:
    class Valves(BaseModel):
        TEST_DB_DSN: str = Field(
            default="postgresql://readonly_user:readonly_pass@test-db:5432/testdb",
            description="Read-only DSN тестовой (синтетической) БД",
            json_schema_extra={"input": {"type": "password"}},
        )
        PROD_DB_DSN: str = Field(
            default="",
            description="Read-only DSN боевой БД QC-мониторинга",
            json_schema_extra={"input": {"type": "password"}},
        )

    def __init__(self):
        self.valves = self.Valves()

    async def plot_qc_data(
        self,
        sql: str,
        chart_type: Literal["bar", "line", "pie", "scatter"] = "bar",
        x_col: Optional[str] = None,
        y_col: Optional[str] = None,
        title: str = "",
        connection: Literal["test", "prod"] = "test",
        row_limit: int = 500,
        __event_emitter__=None,
    ) -> str:
        """
        Строит ОДИН график по агрегирующему SELECT-запросу. Если нужно несколько
        графиков сразу — используй plot_qc_charts, а не несколько вызовов этого тула
        подряд (иначе они могут наложиться друг на друга в чате).

        :param sql: агрегирующий SQL-запрос (включая WITH ... SELECT)
        :param chart_type: тип графика: bar, line, pie, scatter
        :param x_col: колонка для оси X / категорий (по умолчанию — первая колонка результата)
        :param y_col: колонка для оси Y / значений (по умолчанию — вторая колонка результата)
        :param title: заголовок графика
        :param connection: к какой БД обращаться: test (синтетическая) или prod (боевая)
        :param row_limit: максимум строк, которые попадут в график
        """
        dsn = {"test": self.valves.TEST_DB_DSN, "prod": self.valves.PROD_DB_DSN}[
            connection
        ]
        if not dsn:
            return f"Подключение '{connection}' не настроено — пустой DSN в Valves."

        block = render_chart_block(dsn, sql, chart_type, x_col, y_col, title, row_limit)
        html = block + RESIZE_SCRIPT

        if __event_emitter__:
            await __event_emitter__(
                {"type": "embeds", "data": {"embeds": [html], "replace": False}}
            )
            return "График построен и вставлен в чат выше."
        return html

    async def plot_qc_charts(
        self,
        charts: List[dict],
        connection: Literal["test", "prod"] = "test",
        row_limit: int = 500,
        __event_emitter__=None,
    ) -> str:
        """
        Строит ЛЮБОЕ количество графиков (не ограничено) за один вызов и вставляет
        их все одним блоком в чат. Используй этот тул, когда пользователь просит
        построить два и более графика сразу — так они не наложатся друг на друга.

        :param charts: список графиков произвольной длины. Каждый элемент — объект вида
            {"sql": "SELECT ...", "chart_type": "bar", "title": "...", "x_col": "...", "y_col": "..."}.
            Обязательно только поле sql, остальные — опциональны (chart_type по умолчанию "bar").
            Пример: [
              {"sql": "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status", "chart_type": "bar", "title": "Заказы по статусам"},
              {"sql": "SELECT category, SUM(price) AS revenue FROM products GROUP BY category", "chart_type": "pie", "title": "Выручка по категориям"}
            ]
        :param connection: к какой БД обращаться: test или prod
        :param row_limit: лимит строк на каждый график
        """
        if not charts:
            return "Список графиков пуст."

        dsn = {"test": self.valves.TEST_DB_DSN, "prod": self.valves.PROD_DB_DSN}[
            connection
        ]
        if not dsn:
            return f"Подключение '{connection}' не настроено — пустой DSN в Valves."

        blocks = []
        for spec in charts:
            sql = (spec.get("sql") or "").strip()
            if not sql:
                continue
            blocks.append(
                render_chart_block(
                    dsn,
                    sql,
                    spec.get("chart_type", "bar"),
                    spec.get("x_col"),
                    spec.get("y_col"),
                    spec.get("title", ""),
                    row_limit,
                )
            )

        if not blocks:
            return "Ни в одном элементе списка не передан непустой SQL."

        html = "".join(blocks) + RESIZE_SCRIPT

        if __event_emitter__:
            await __event_emitter__(
                {"type": "embeds", "data": {"embeds": [html], "replace": False}}
            )
            return (
                f"Построено графиков: {len(blocks)}. Вставлены в чат одним блоком выше."
            )
        return "Ошибка: __event_emitter__ недоступен."
