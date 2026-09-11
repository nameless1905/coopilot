"""
title: QC Database Reader
requirements: psycopg2-binary,sqlglot
version: 0.3.0
"""

from typing import Literal
from pydantic import BaseModel, Field
import psycopg2
import sqlglot
from sqlglot import exp

FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Alter, exp.Create)


def is_safe_select(sql: str) -> tuple[bool, str]:
    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except Exception as e:
        return False, f"Не удалось разобрать SQL: {e}"
    if len(statements) != 1:
        return False, "Разрешён ровно один SQL-оператор за запрос."
    stmt = statements[0]
    if stmt is None or not isinstance(stmt, exp.Select):
        return False, "Верхний уровень запроса должен быть SELECT (включая WITH ... SELECT)."
    if list(stmt.find_all(*FORBIDDEN)):
        return False, "Обнаружена операция изменения данных внутри запроса — запрещено."
    return True, ""


class Tools:
    class Valves(BaseModel):
        TEST_DB_DSN: str = Field(
            default="postgresql://readonly_user:readonly_pass@localhost:5433/testdb",
            description="Read-only DSN тестовой (синтетической) БД",
            json_schema_extra={"input": {"type": "password"}},
        )
        PROD_DB_DSN: str = Field(
            default="",
            description="Read-only DSN боевой БД QC-мониторинга (заполнить, когда будет готова)",
            json_schema_extra={"input": {"type": "password"}},
        )

    def __init__(self):
        self.valves = self.Valves()

    def query_qc_data(
        self,
        sql: str,
        connection: Literal["test", "prod"] = "test",
        row_limit: int = 200,
    ) -> str:
        """
        Выполняет SELECT (в т.ч. WITH ... SELECT) к выбранной БД.
        :param sql: SQL-запрос
        :param connection: к какой БД обращаться: test (синтетическая) или prod (боевая)
        :param row_limit: максимум строк в ответе
        """
        dsn = {"test": self.valves.TEST_DB_DSN, "prod": self.valves.PROD_DB_DSN}[connection]
        if not dsn:
            return f"Подключение '{connection}' не настроено — пустой DSN в Valves."

        ok, reason = is_safe_select(sql)
        if not ok:
            return f"Запрос отклонён: {reason}"

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

            header = "| " + " | ".join(cols) + " |"
            sep = "|" + "---|" * len(cols)
            body = "\n".join("| " + " | ".join(map(str, r)) + " |" for r in rows)
            return f"[{connection}] {header}\n{sep}\n{body}"
        except Exception as e:
            return f"Ошибка запроса к '{connection}': {e}"