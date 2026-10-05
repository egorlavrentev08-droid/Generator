import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatMemberUpdated,
)
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

# ============================================================
#                        КОНФИГИ
# ============================================================
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

with open(BASE_DIR / "Codes.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

LANGS = CONFIG["languages"]
MSG = CONFIG["messages"]

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not BOT_TOKEN:
    raise SystemExit("❌ Заполни BOT_TOKEN в .env")

ACCESS_FILE = BASE_DIR / "access.json"


def load_allowed() -> set:
    allowed = {OWNER_ID}
    if ACCESS_FILE.exists():
        try:
            data = json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
            allowed.update(int(x) for x in data.get("allowed", []))
        except Exception:
            logging.exception("access.json не прочитан")
    return allowed


def save_allowed(ids: set) -> None:
    ACCESS_FILE.write_text(
        json.dumps({"allowed": sorted(ids - {OWNER_ID})}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


ALLOWED = load_allowed()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ============================================================
#                  ТИПЫ ПЕРЕМЕННЫХ (маппинг)
# ============================================================
PY_TYPES = {"INT": "int", "FLOAT": "float", "STR": "str", "BOOL": "bool"}
CPP_TYPES = {"INT": "int", "FLOAT": "double", "STR": "string", "BOOL": "bool"}
JAVA_TYPES = {"INT": "int", "FLOAT": "double", "STR": "String", "BOOL": "boolean"}
VB_TYPES = {"INT": "Integer", "FLOAT": "Double", "STR": "String", "BOOL": "Boolean"}
PY_INPUT = {"INT": "int", "FLOAT": "float", "STR": "str", "BOOL": "bool"}


# ============================================================
#                       AST-УЗЛЫ
# ============================================================
@dataclass
class Node:
    pass


@dataclass
class VarDecl(Node):
    names: list
    vtype: str
    size: int = 0


@dataclass
class Input(Node):
    names: list


@dataclass
class Print(Node):
    exprs: list


@dataclass
class Assign(Node):
    target: str
    expr: str


@dataclass
class If(Node):
    cond: str
    then_body: list = field(default_factory=list)
    else_body: list = field(default_factory=list)


@dataclass
class For(Node):
    var: str
    start: str
    end: str
    body: list = field(default_factory=list)


@dataclass
class While(Node):
    cond: str
    body: list = field(default_factory=list)


@dataclass
class FuncDef(Node):
    name: str
    params: list
    ret_type: str
    body: list = field(default_factory=list)


@dataclass
class Return(Node):
    expr: str = ""


@dataclass
class CallStmt(Node):
    call_expr: str


# ============================================================
#                     ПАРСЕР СХЕМЫ
# ============================================================
class ParseError(Exception):
    def __init__(self, line: int, msg: str):
        super().__init__(f"строка {line}: {msg}")
        self.line = line
        self.msg = msg


def split_top_level(s: str, sep: str = ","):
    parts, buf, depth, in_str = [], "", 0, False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '"' and (i == 0 or s[i - 1] != "\\"):
            in_str = not in_str
        if not in_str:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == sep and depth == 0:
                parts.append(buf.strip())
                buf = ""
                i += 1
                continue
        buf += ch
        i += 1
    if buf.strip():
        parts.append(buf.strip())
    return parts


def parse_scheme(text: str):
    raw_lines = text.splitlines()
    tokens = []
    for i, raw in enumerate(raw_lines, 1):
        line = raw.rstrip()
        out, in_str = "", False
        j = 0
        while j < len(line):
            ch = line[j]
            if ch == '"' and (j == 0 or line[j - 1] != "\\"):
                in_str = not in_str
            if ch == "#" and not in_str:
                break
            out += ch
            j += 1
        line = out.strip()
        if not line:
            continue
        up = line.upper()
        if up in ("BEGIN", "END"):
            continue
        tokens.append((i, line))

    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def next_tok():
        nonlocal pos
        if pos >= len(tokens):
            raise ParseError(tokens[-1][0] if tokens else 0, "неожиданный конец схемы")
        t = tokens[pos]
        pos += 1
        return t

    def parse_block(stop_words):
        body = []
        while True:
            t = peek()
            if t is None:
                if stop_words:
                    raise ParseError(tokens[-1][0] if tokens else 0,
                                     f"ожидалось {stop_words}, схема закончилась")
                return body
            line_no, line = t
            up = line.upper()

            if up in stop_words:
                return body

            # FUNCTION name(a: INT, b: INT): INT
            m = re.match(
                r"^FUNCTION\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*:\s*(\w+)$",
                line, re.IGNORECASE,
            )
            if m:
                next_tok()
                name = m.group(1)
                params_str = m.group(2).strip()
                ret = m.group(3).upper()
                params = []
                if params_str:
                    for p in split_top_level(params_str):
                        pm = re.match(r"^([A-Za-z_]\w*)\s*:\s*(\w+)$", p)
                        if not pm:
                            raise ParseError(line_no, f"плохой параметр: {p!r}")
                        params.append((pm.group(1), pm.group(2).upper()))
                body_f = parse_block({"ENDFUNCTION"})
                t2 = peek()
                if not t2 or t2[1].upper() != "ENDFUNCTION":
                    raise ParseError(line_no, "не найден ENDFUNCTION")
                next_tok()
                body.append(FuncDef(name, params, ret, body_f))
                continue

            # VAR a[10]: INT  — массив (проверяем первым, т.к. более специфично)
            m = re.match(r"^VAR\s+([A-Za-z_]\w*)\s*\[\s*(\d+)\s*\]\s*:\s*(\w+)$",
                         line, re.IGNORECASE)
            if m:
                next_tok()
                body.append(VarDecl([m.group(1)], m.group(3).upper(), int(m.group(2))))
                continue

            # VAR x, y, z: INT
            m = re.match(r"^VAR\s+(.+?)\s*:\s*(\w+)$", line, re.IGNORECASE)
            if m:
                next_tok()
                names = [x.strip() for x in split_top_level(m.group(1)) if x.strip()]
                vtype = m.group(2).upper()
                if vtype not in PY_TYPES:
                    raise ParseError(line_no, f"неизвестный тип: {vtype}")
                body.append(VarDecl(names, vtype))
                continue

            # INPUT
            m = re.match(r"^INPUT\s+(.+)$", line, re.IGNORECASE)
            if m:
                next_tok()
                names = [n.strip() for n in split_top_level(m.group(1)) if n.strip()]
                body.append(Input(names))
                continue

            # PRINT
            m = re.match(r"^PRINT\s+(.+)$", line, re.IGNORECASE)
            if m:
                next_tok()
                body.append(Print([e for e in split_top_level(m.group(1)) if e]))
                continue

            # RETURN [expr]
            m = re.match(r"^RETURN(?:\s+(.+))?$", line, re.IGNORECASE)
            if m:
                next_tok()
                body.append(Return((m.group(1) or "").strip()))
                continue

            # IF ... THEN
            m = re.match(r"^IF\s+(.+?)\s+THEN$", line, re.IGNORECASE)
            if m:
                next_tok()
                cond = m.group(1).strip()
                then_body = parse_block({"ELSE", "ENDIF"})
                else_body = []
                t2 = peek()
                if t2 and t2[1].upper() == "ELSE":
                    next_tok()
                    else_body = parse_block({"ENDIF"})
                t3 = peek()
                if not t3 or t3[1].upper() != "ENDIF":
                    raise ParseError(line_no, "не найден ENDIF")
                next_tok()
                body.append(If(cond, then_body, else_body))
                continue

            # FOR i FROM a TO b
            m = re.match(r"^FOR\s+([A-Za-z_]\w*)\s+FROM\s+(.+?)\s+TO\s+(.+)$",
                         line, re.IGNORECASE)
            if m:
                next_tok()
                inner = parse_block({"ENDFOR"})
                t2 = peek()
                if not t2 or t2[1].upper() != "ENDFOR":
                    raise ParseError(line_no, "не найден ENDFOR")
                next_tok()
                body.append(For(m.group(1), m.group(2).strip(), m.group(3).strip(), inner))
                continue

            # WHILE cond
            m = re.match(r"^WHILE\s+(.+)$", line, re.IGNORECASE)
            if m:
                next_tok()
                inner = parse_block({"ENDWHILE"})
                t2 = peek()
                if not t2 or t2[1].upper() != "ENDWHILE":
                    raise ParseError(line_no, "не найден ENDWHILE")
                next_tok()
                body.append(While(m.group(1).strip(), inner))
                continue

            # Присваивание: x = ...  / a[i] = ...
            m = re.match(r"^([A-Za-z_]\w*(?:\s*\[[^\]]+\])?)\s*=\s*(.+)$", line)
            if m and not up.startswith(("IF ", "WHILE ", "FOR ", "VAR ", "FUNCTION ", "RETURN", "PRINT", "INPUT")):
                next_tok()
                body.append(Assign(m.group(1).replace(" ", ""), m.group(2).strip()))
                continue

            # Вызов процедуры: MYPROC(x, y)
            m = re.match(r"^([A-Za-z_]\w*)\s*\((.*)\)\s*$", line)
            if m:
                next_tok()
                body.append(CallStmt(line))
                continue

            raise ParseError(line_no, f"не понимаю команду: {line!r}")

    ast = parse_block(set())
    if not ast:
        raise ParseError(0, "пустая схема")
    return ast


# ============================================================
#             ТРАНСЛЯЦИЯ ВЫРАЖЕНИЙ ПОД ЯЗЫК
# ============================================================
def translate_expr(expr: str, lang: str) -> str:
    s = expr.strip()
    strings = []

    def mask(m):
        strings.append(m.group(0))
        return f"\x00{len(strings) - 1}\x00"

    s = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', mask, s)

    s = re.sub(r"\bAND\b", "__AND__", s, flags=re.IGNORECASE)
    s = re.sub(r"\bOR\b", "__OR__", s, flags=re.IGNORECASE)
    s = re.sub(r"\bNOT\b", "__NOT__", s, flags=re.IGNORECASE)
    s = re.sub(r"\bTRUE\b", "__TRUE__", s, flags=re.IGNORECASE)
    s = re.sub(r"\bFALSE\b", "__FALSE__", s, flags=re.IGNORECASE)
    s = re.sub(r"\bMOD\b", "%", s, flags=re.IGNORECASE)
    if lang == "python":
        s = re.sub(r"\bDIV\b", "//", s, flags=re.IGNORECASE)
    else:
        s = re.sub(r"\bDIV\b", "/", s, flags=re.IGNORECASE)

    if lang == "python":
        s = s.replace("__AND__", "and").replace("__OR__", "or").replace("__NOT__", "not")
        s = s.replace("__TRUE__", "True").replace("__FALSE__", "False")
    elif lang in ("cpp", "java"):
        s = s.replace("__AND__", "&&").replace("__OR__", "||").replace("__NOT__", "!")
        s = s.replace("__TRUE__", "true").replace("__FALSE__", "false")
    elif lang == "vb":
        s = s.replace("__AND__", "And").replace("__OR__", "Or").replace("__NOT__", "Not")
        s = s.replace("__TRUE__", "True").replace("__FALSE__", "False")

    def unmask(m):
        return strings[int(m.group(1))]

    s = re.sub(r"\x00(\d+)\x00", unmask, s)
    return s


def is_string_literal(e: str) -> bool:
    e = e.strip()
    return len(e) >= 2 and e[0] == '"' and e[-1] == '"'


# ============================================================
#                 ГЕНЕРАТОРЫ КОДА
# ============================================================
class BaseGen:
    def __init__(self):
        self.lines = []
        self.indent = 0
        self.vars = {}
        self.funcs = {}

    def w(self, text=""):
        self.lines.append(("    " * self.indent) + text if text else "")

    def collect(self, ast):
        for n in ast:
            if isinstance(n, VarDecl):
                for name in n.names:
                    self.vars[name] = n.vtype
            elif isinstance(n, FuncDef):
                self.funcs[n.name] = (n.params, n.ret_type)
                for pname, ptype in n.params:
                    self.vars.setdefault(pname, ptype)
                self.collect(n.body)
            elif isinstance(n, If):
                self.collect(n.then_body)
                self.collect(n.else_body)
            elif isinstance(n, For):
                self.vars.setdefault(n.var, "INT")
                self.collect(n.body)
            elif isinstance(n, While):
                self.collect(n.body)

    def type_of(self, name):
        return self.vars.get(name, "INT")

    def gen(self, ast):
        self.collect(ast)
        return self._render(ast)


# ------------------------- PYTHON -------------------------
class PythonGen(BaseGen):
    def _render(self, ast):
        funcs = [n for n in ast if isinstance(n, FuncDef)]
        main = [n for n in ast if not isinstance(n, FuncDef)]

        out = ["# Автоматически сгенерировано из схемы алгоритма", ""]

        for f in funcs:
            out.extend(self.render_func(f))
            out.append("")

        out.append("def main():")
        self.lines = []
        self.indent = 1
        for n in main:
            self.node(n)
        if not self.lines:
            self.lines = ["    pass"]
        out.extend(self.lines)
        out.append("")
        out.append("")
        out.append('if __name__ == "__main__":')
        out.append("    main()")
        return "\n".join(out)

    def render_func(self, f):
        params = ", ".join(p[0] for p in f.params)
        self.lines = []
        self.indent = 1
        for n in f.body:
            self.node(n)
        if not self.lines:
            self.lines = ["    pass"]
        return [f"def {f.name}({params}):"] + self.lines

    def node(self, n):
        if isinstance(n, VarDecl):
            if n.size > 0:
                self.w(f"{n.names[0]} = [0] * {n.size}")
            return
        if isinstance(n, Input):
            for name in n.names:
                t = self.type_of(name)
                fn = PY_INPUT.get(t, "int")
                self.w(f'{name} = {fn}(input("Введите {name}: "))')
            return
        if isinstance(n, Assign):
            self.w(f"{n.target} = {translate_expr(n.expr, 'python')}")
            return
        if isinstance(n, Print):
            parts = []
            for e in n.exprs:
                if is_string_literal(e):
                    parts.append(e)
                else:
                    parts.append(f"str({translate_expr(e, 'python')})")
            self.w("print(" + ", ".join(parts) + ")")
            return
        if isinstance(n, Return):
            if n.expr:
                self.w(f"return {translate_expr(n.expr, 'python')}")
            else:
                self.w("return")
            return
        if isinstance(n, CallStmt):
            self.w(translate_expr(n.call_expr, "python"))
            return
        if isinstance(n, If):
            self.w(f"if {translate_expr(n.cond, 'python')}:")
            self.indent += 1
            for x in n.then_body:
                self.node(x)
            self.indent -= 1
            if n.else_body:
                self.w("else:")
                self.indent += 1
                for x in n.else_body:
                    self.node(x)
                self.indent -= 1
            return
        if isinstance(n, For):
            a = translate_expr(n.start, "python")
            b = translate_expr(n.end, "python")
            self.w(f"for {n.var} in range({a}, {b} + 1):")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            return
        if isinstance(n, While):
            self.w(f"while {translate_expr(n.cond, 'python')}:")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            return


# ------------------------- C++ -------------------------
class CppGen(BaseGen):
    def _render(self, ast):
        funcs = [n for n in ast if isinstance(n, FuncDef)]
        main = [n for n in ast if not isinstance(n, FuncDef)]

        out = [
            "// Автоматически сгенерировано из схемы алгоритма",
            "#include <iostream>",
            "#include <string>",
            "using namespace std;",
            "",
        ]

        for f in funcs:
            params = ", ".join(f"{CPP_TYPES.get(p[1], 'int')} {p[0]}" for p in f.params)
            ret = "void" if f.ret_type == "VOID" else CPP_TYPES.get(f.ret_type, "int")
            out.append(f"{ret} {f.name}({params});")
        if funcs:
            out.append("")

        for f in funcs:
            params = ", ".join(f"{CPP_TYPES.get(p[1], 'int')} {p[0]}" for p in f.params)
            ret = "void" if f.ret_type == "VOID" else CPP_TYPES.get(f.ret_type, "int")
            out.append(f"{ret} {f.name}({params}) {{")
            for n in f.body:
                if isinstance(n, VarDecl):
                    for name in n.names:
                        if n.size > 0:
                            out.append(f"    {CPP_TYPES.get(n.vtype, 'int')} {name}[{n.size}];")
                        else:
                            out.append(f"    {CPP_TYPES.get(n.vtype, 'int')} {name};")
            self.lines = []
            self.indent = 1
            for n in f.body:
                if not isinstance(n, VarDecl):
                    self.node(n)
            out.extend(self.lines)
            out.append("}")
            out.append("")

        out.append("int main() {")
        for n in main:
            if isinstance(n, VarDecl):
                for name in n.names:
                    if n.size > 0:
                        out.append(f"    {CPP_TYPES.get(n.vtype, 'int')} {name}[{n.size}];")
                    else:
                        out.append(f"    {CPP_TYPES.get(n.vtype, 'int')} {name};")
        self.lines = []
        self.indent = 1
        for n in main:
            if not isinstance(n, VarDecl):
                self.node(n)
        out.extend(self.lines)
        out.append("    return 0;")
        out.append("}")
        return "\n".join(out)

    def node(self, n):
        if isinstance(n, VarDecl):
            return
        if isinstance(n, Input):
            for name in n.names:
                self.w(f'cout << "Введите {name}: ";')
                self.w(f"cin >> {name};")
            return
        if isinstance(n, Assign):
            self.w(f"{n.target} = {translate_expr(n.expr, 'cpp')};")
            return
        if isinstance(n, Print):
            parts = []
            for e in n.exprs:
                if is_string_literal(e):
                    parts.append(e)
                else:
                    parts.append(translate_expr(e, "cpp"))
            self.w("cout << " + " << \" \" << ".join(parts) + " << endl;")
            return
        if isinstance(n, Return):
            if n.expr:
                self.w(f"return {translate_expr(n.expr, 'cpp')};")
            else:
                self.w("return;")
            return
        if isinstance(n, CallStmt):
            self.w(translate_expr(n.call_expr, "cpp") + ";")
            return
        if isinstance(n, If):
            self.w(f"if ({translate_expr(n.cond, 'cpp')}) {{")
            self.indent += 1
            for x in n.then_body:
                self.node(x)
            self.indent -= 1
            if n.else_body:
                self.w("} else {")
                self.indent += 1
                for x in n.else_body:
                    self.node(x)
                self.indent -= 1
            self.w("}")
            return
        if isinstance(n, For):
            a = translate_expr(n.start, "cpp")
            b = translate_expr(n.end, "cpp")
            self.w(f"for (int {n.var} = {a}; {n.var} <= {b}; {n.var}++) {{")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("}")
            return
        if isinstance(n, While):
            self.w(f"while ({translate_expr(n.cond, 'cpp')}) {{")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("}")
            return


# ------------------------- JAVA -------------------------
class JavaGen(BaseGen):
    def _render(self, ast):
        funcs = [n for n in ast if isinstance(n, FuncDef)]
        main = [n for n in ast if not isinstance(n, FuncDef)]

        out = [
            "// Автоматически сгенерировано из схемы алгоритма",
            "import java.util.Scanner;",
            "",
            "public class Main {",
        ]

        for f in funcs:
            params = ", ".join(f"{JAVA_TYPES.get(p[1], 'int')} {p[0]}" for p in f.params)
            ret = "void" if f.ret_type == "VOID" else JAVA_TYPES.get(f.ret_type, "int")
            out.append(f"    static {ret} {f.name}({params}) {{")
            for n in f.body:
                if isinstance(n, VarDecl):
                    for name in n.names:
                        if n.size > 0:
                            out.append(
                                f"        {JAVA_TYPES.get(n.vtype, 'int')}[] {name} = "
                                f"new {JAVA_TYPES.get(n.vtype, 'int')}[{n.size}];"
                            )
                        else:
                            out.append(f"        {JAVA_TYPES.get(n.vtype, 'int')} {name};")
            self.lines = []
            self.indent = 2
            for n in f.body:
                if not isinstance(n, VarDecl):
                    self.node(n)
            out.extend(self.lines)
            out.append("    }")
            out.append("")

        out.append("    public static void main(String[] args) {")
        out.append("        Scanner sc = new Scanner(System.in);")
        for n in main:
            if isinstance(n, VarDecl):
                for name in n.names:
                    if n.size > 0:
                        out.append(
                            f"        {JAVA_TYPES.get(n.vtype, 'int')}[] {name} = "
                            f"new {JAVA_TYPES.get(n.vtype, 'int')}[{n.size}];"
                        )
                    else:
                        out.append(f"        {JAVA_TYPES.get(n.vtype, 'int')} {name};")
        self.lines = []
        self.indent = 2
        for n in main:
            if not isinstance(n, VarDecl):
                self.node(n)
        out.extend(self.lines)
        out.append("        sc.close();")
        out.append("    }")
        out.append("}")
        return "\n".join(out)

    def node(self, n):
        if isinstance(n, VarDecl):
            return
        if isinstance(n, Input):
            for name in n.names:
                t = self.type_of(name)
                self.w(f'System.out.print("Введите {name}: ");')
                if t == "INT":
                    self.w(f"{name} = sc.nextInt();")
                elif t == "FLOAT":
                    self.w(f"{name} = sc.nextDouble();")
                elif t == "BOOL":
                    self.w(f"{name} = sc.nextBoolean();")
                else:
                    self.w(f"{name} = sc.next();")
            return
        if isinstance(n, Assign):
            self.w(f"{n.target} = {translate_expr(n.expr, 'java')};")
            return
        if isinstance(n, Print):
            parts = []
            for e in n.exprs:
                if is_string_literal(e):
                    parts.append(e)
                else:
                    parts.append(translate_expr(e, "java"))
            self.w("System.out.println(" + " + \" \" + ".join(parts) + ");")
            return
        if isinstance(n, Return):
            if n.expr:
                self.w(f"return {translate_expr(n.expr, 'java')};")
            else:
                self.w("return;")
            return
        if isinstance(n, CallStmt):
            self.w(translate_expr(n.call_expr, "java") + ";")
            return
        if isinstance(n, If):
            self.w(f"if ({translate_expr(n.cond, 'java')}) {{")
            self.indent += 1
            for x in n.then_body:
                self.node(x)
            self.indent -= 1
            if n.else_body:
                self.w("} else {")
                self.indent += 1
                for x in n.else_body:
                    self.node(x)
                self.indent -= 1
            self.w("}")
            return
        if isinstance(n, For):
            a = translate_expr(n.start, "java")
            b = translate_expr(n.end, "java")
            self.w(f"for (int {n.var} = {a}; {n.var} <= {b}; {n.var}++) {{")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("}")
            return
        if isinstance(n, While):
            self.w(f"while ({translate_expr(n.cond, 'java')}) {{")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("}")
            return


# ------------------------- VISUAL BASIC -------------------------
class VbGen(BaseGen):
    def _render(self, ast):
        funcs = [n for n in ast if isinstance(n, FuncDef)]
        main = [n for n in ast if not isinstance(n, FuncDef)]

        out = [
            "' Автоматически сгенерировано из схемы алгоритма",
            "Imports System",
            "",
            "Module Program",
        ]

        for f in funcs:
            params = ", ".join(
                f"ByVal {p[0]} As {VB_TYPES.get(p[1], 'Integer')}" for p in f.params
            )
            ret = "" if f.ret_type == "VOID" else f" As {VB_TYPES.get(f.ret_type, 'Integer')}"
            out.append(f"    Function {f.name}({params}){ret}")
            for n in f.body:
                if isinstance(n, VarDecl):
                    for name in n.names:
                        if n.size > 0:
                            out.append(
                                f"        Dim {name}({n.size - 1}) As "
                                f"{VB_TYPES.get(n.vtype, 'Integer')}"
                            )
                        else:
                            out.append(
                                f"        Dim {name} As {VB_TYPES.get(n.vtype, 'Integer')}"
                            )
            self.lines = []
            self.indent = 2
            for n in f.body:
                if not isinstance(n, VarDecl):
                    self.node(n)
            out.extend(self.lines)
            out.append("    End Function")
            out.append("")

        out.append("    Sub Main()")
        for n in main:
            if isinstance(n, VarDecl):
                for name in n.names:
                    if n.size > 0:
                        out.append(
                            f"        Dim {name}({n.size - 1}) As "
                            f"{VB_TYPES.get(n.vtype, 'Integer')}"
                        )
                    else:
                        out.append(
                            f"        Dim {name} As {VB_TYPES.get(n.vtype, 'Integer')}"
                        )
        self.lines = []
        self.indent = 2
        for n in main:
            if not isinstance(n, VarDecl):
                self.node(n)
        out.extend(self.lines)
        out.append("    End Sub")
        out.append("End Module")
        return "\n".join(out)

    def node(self, n):
        if isinstance(n, VarDecl):
            return
        if isinstance(n, Input):
            for name in n.names:
                t = self.type_of(name)
                self.w(f'Console.Write("Введите {name}: ")')
                if t == "INT":
                    self.w(f"{name} = Integer.Parse(Console.ReadLine())")
                elif t == "FLOAT":
                    self.w(f"{name} = Double.Parse(Console.ReadLine())")
                elif t == "BOOL":
                    self.w(f"{name} = Boolean.Parse(Console.ReadLine())")
                else:
                    self.w(f"{name} = Console.ReadLine()")
            return
        if isinstance(n, Assign):
            self.w(f"{n.target} = {translate_expr(n.expr, 'vb')}")
            return
        if isinstance(n, Print):
            parts = []
            for e in n.exprs:
                if is_string_literal(e):
                    parts.append(e)
                else:
                    parts.append(f"CStr({translate_expr(e, 'vb')})")
            self.w("Console.WriteLine(" + " & \" \" & ".join(parts) + ")")
            return
        if isinstance(n, Return):
            if n.expr:
                self.w(f"Return {translate_expr(n.expr, 'vb')}")
            else:
                self.w("Return")
            return
        if isinstance(n, CallStmt):
            self.w(translate_expr(n.call_expr, "vb"))
            return
        if isinstance(n, If):
            self.w(f"If {translate_expr(n.cond, 'vb')} Then")
            self.indent += 1
            for x in n.then_body:
                self.node(x)
            self.indent -= 1
            if n.else_body:
                self.w("Else")
                self.indent += 1
                for x in n.else_body:
                    self.node(x)
                self.indent -= 1
            self.w("End If")
            return
        if isinstance(n, For):
            a = translate_expr(n.start, "vb")
            b = translate_expr(n.end, "vb")
            self.w(f"For {n.var} = {a} To {b}")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("Next")
            return
        if isinstance(n, While):
            self.w(f"While {translate_expr(n.cond, 'vb')}")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("End While")
            return


# ============================================================
#                    ВЫБОР ГЕНЕРАТОРА
# ============================================================
GENERATORS = {
    "python": PythonGen,
    "java": JavaGen,
    "cpp": CppGen,
    "vb": VbGen,
}


def generate_code(scheme: str, lang: str) -> str:
    ast = parse_scheme(scheme)
    return GENERATORS[lang]().gen(ast)


# ============================================================
#                        БОТ
# ============================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
user_lang: dict = {}


def is_allowed(uid: int) -> bool:
    return uid in ALLOWED


def langs_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=d["title"], callback_data=d["callback"])]
        for d in LANGS.values()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.my_chat_member()
async def on_added_to_chat(event: ChatMemberUpdated):
    if event.chat.type in ("group", "supergroup", "channel") and event.new_chat_member.status in (
        "member", "administrator"
    ):
        try:
            await bot.send_message(event.chat.id, MSG["left_chat"])
        except TelegramBadRequest:
            pass
        try:
            await bot.leave_chat(event.chat.id)
            logging.info("Покинул чат %s", event.chat.id)
        except TelegramBadRequest:
            logging.exception("Не смог выйти из %s", event.chat.id)


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Твой ID: <code>{m.from_user.id}</code>")


@dp.message(Command("add"))
async def cmd_add(m: Message, c: CommandObject):
    if m.from_user.id != OWNER_ID:
        await m.answer(MSG["only_owner"]); return
    arg = (c.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await m.answer(MSG["add_usage"]); return
    uid = int(arg)
    ALLOWED.add(uid); save_allowed(ALLOWED)
    await m.answer(MSG["added"].format(user_id=uid))


@dp.message(Command("remove"))
async def cmd_remove(m: Message, c: CommandObject):
    if m.from_user.id != OWNER_ID:
        await m.answer(MSG["only_owner"]); return
    arg = (c.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await m.answer("Использование: <code>/remove 123456789</code>"); return
    uid = int(arg)
    if uid == OWNER_ID:
        await m.answer("Нельзя удалить владельца."); return
    ALLOWED.discard(uid); save_allowed(ALLOWED)
    await m.answer(MSG["removed"].format(user_id=uid))


@dp.message(Command("list"))
async def cmd_list(m: Message):
    if m.from_user.id != OWNER_ID:
        await m.answer(MSG["only_owner"]); return
    others = sorted(ALLOWED - {OWNER_ID})
    if not others:
        await m.answer(MSG["empty_list"]); return
    await m.answer(MSG["list"].format(users="\n".join(f"• <code>{u}</code>" for u in others)))


@dp.message(Command("start"))
async def cmd_start(m: Message):
    if not is_allowed(m.from_user.id):
        await m.answer(MSG["no_access"]); return
    user_lang.pop(m.from_user.id, None)
    await m.answer(MSG["start"], reply_markup=langs_keyboard())


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    if not is_allowed(m.from_user.id):
        await m.answer(MSG["no_access"]); return
    user_lang.pop(m.from_user.id, None)
    await m.answer("Отменено. /start — начать заново.")


@dp.callback_query(F.data.startswith("lang_"))
async def on_lang(call: CallbackQuery):
    if not is_allowed(call.from_user.id):
        await call.answer(MSG["no_access"], show_alert=True); return
    for key, d in LANGS.items():
        if d["callback"] == call.data:
            user_lang[call.from_user.id] = key
            await call.message.edit_text(
                f"✅ Выбран язык: <b>{d['title']}</b>\n\n{MSG['ask_scheme']}"
            )
            await call.answer(); return
    await call.answer("Неизвестный язык", show_alert=True)


@dp.message(F.text)
async def on_scheme(m: Message):
    if not is_allowed(m.from_user.id):
        await m.answer(MSG["no_access"]); return
    lang = user_lang.get(m.from_user.id)
    if not lang:
        await m.answer(MSG["no_lang"], reply_markup=langs_keyboard()); return

    try:
        code = generate_code(m.text, lang)
    except ParseError as e:
        await m.answer(MSG["parse_error"].format(line=e.line, err=e.msg))
        return
    except Exception as e:
        logging.exception("Ошибка генерации")
        await m.answer(f"❌ Ошибка: {e}")
        return

    lang_title = LANGS[lang]["title"]
    header = f"✅ Код на <b>{lang_title}</b>:\n"
    for chunk in split_message(header + "\n<pre>" + escape_html(code) + "</pre>"):
        await m.answer(chunk)


def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def split_message(text: str, limit: int = 4000):
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit:
            parts.append(cur); cur = line
        else:
            cur += line
    if cur:
        parts.append(cur)
    return parts


async def main():
    logging.info("🤖 Бот запущен. Владелец: %s", OWNER_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
