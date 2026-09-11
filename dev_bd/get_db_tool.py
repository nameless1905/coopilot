"""
title: QC Database Reader
requirements: psycopg2-binary,sqlglot
version: 0.2.0
"""

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
        return False, "Разрешён ровно один SQL-оператор за запрос (без ; в конце нескольких команд)."

    stmt = statements[0]
    if stmt is None or not isinstance(stmt, exp.Select):
        return False, "Верхний уровень запроса должен быть SELECT (включая WITH ... SELECT)."

    if list(stmt.find_all(*FORBIDDEN)):
        return False, "Обнаружена операция изменения данных внутри запроса (в т.ч. внутри CTE) — запрещено."

    return True, ""


class Tools:
    class Valves(BaseModel):
        DB_DSN: str = Field(default="postgresql://readonly_user:pass@host:5432/qc_db")

    def __init__(self):
        self.valves = self.Valves()

    def query_qc_data(self, sql: str, row_limit: int = 200) -> str:
        """
        Выполняет только SELECT (в т.ч. с CTE через WITH) к базе QC-мониторинга.
        :param sql: SQL-запрос (SELECT или WITH ... SELECT)
        :param row_limit: максимум строк в ответе
        """
        ok, reason = is_safe_select(sql)
        if not ok:
            return f"Запрос отклонён: {reason}"

        try:
            conn = psycopg2.connect(self.valves.DB_DSN)
            conn.set_session(readonly=True)          # доп. страховка на уровне соединения
            cur = conn.cursor()
            cur.execute("SET statement_timeout = '5000'")   # 5с — обрубаем тяжёлые/рекурсивные CTE
            cur.execute(f"SELECT * FROM ({sql.rstrip(';')}) AS _sub LIMIT {row_limit}")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            cur.close()
            conn.close()

            header = "| " + " | ".join(cols) + " |"
            sep = "|" + "---|" * len(cols)
            body = "\n".join("| " + " | ".join(map(str, r)) + " |" for r in rows)
            return f"{header}\n{sep}\n{body}"
        except Exception as e:
            return f"Ошибка запроса: {e}"
