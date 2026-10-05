import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def load_allowed() -> set[int]:
    allowed = {OWNER_ID}
    if ACCESS_FILE.exists():
        try:
            data = json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
            allowed.update(int(x) for x in data.get("allowed", []))
        except Exception:
            logging.exception("access.json не прочитан")
    return allowed


def save_allowed(ids: set[int]) -> None:
    ACCESS_FILE.write_text(
        json.dumps({"allowed": sorted(ids - {OWNER_ID})}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


ALLOWED = load_allowed()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ============================================================
#                   1. ПАРСЕР СХЕМЫ (AST)
# ============================================================

class ParseError(Exception):
    def __init__(self, line: int, msg: str):
        super().__init__(f"строка {line}: {msg}")
        self.line = line
        self.msg = msg


@dataclass
class Node:  # базовый узел AST
    pass


@dataclass
class Input(Node):
    names: list[str]


@dataclass
class Print(Node):
    exprs: list[str]


@dataclass
class Assign(Node):
    name: str
    expr: str


@dataclass
class If(Node):
    cond: str
    then_body: list[Node] = field(default_factory=list)
    else_body: list[Node] = field(default_factory=list)


@dataclass
class For(Node):
    var: str
    start: str
    end: str
    body: list[Node] = field(default_factory=list)


@dataclass
class While(Node):
    cond: str
    body: list[Node] = field(default_factory=list)


def split_top_level(s: str, sep: str = ",") -> list[str]:
    """Разбивает строку по запятым, не трогая запятые внутри кавычек/скобок."""
    parts, buf, depth, in_str = [], "", 0, False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '"' and (i == 0 or s[i - 1] != "\\"):
            in_str = not in_str
        if not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
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


def parse_scheme(text: str) -> list[Node]:
    lines = text.splitlines()

    # токенизация: убираем BEGIN/END, комментарии, пустые строки
    tokens: list[tuple[int, str]] = []
    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # комментарий в конце строки
        if "#" in line:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
        up = line.upper()
        if up in ("BEGIN", "END"):
            continue
        tokens.append((i, line))

    pos = 0

    def peek() -> tuple[int, str] | None:
        return tokens[pos] if pos < len(tokens) else None

    def next_tok() -> tuple[int, str]:
        nonlocal pos
        if pos >= len(tokens):
            raise ParseError(tokens[-1][0] if tokens else 0, "неожиданный конец схемы")
        t = tokens[pos]
        pos += 1
        return t

    def parse_block(stop_words: set[str]) -> list[Node]:
        body: list[Node] = []
        while True:
            t = peek()
            if t is None:
                if stop_words:
                    raise ParseError(tokens[-1][0] if tokens else 0,
                                     f"ожидалось {stop_words}, но схема закончилась")
                return body
            line_no, line = t
            up = line.upper()

            if up in stop_words:
                return body

            # INPUT
            m = re.match(r"^INPUT\s+(.+)$", line, re.IGNORECASE)
            if m:
                next_tok()
                names = [n.strip() for n in split_top_level(m.group(1)) if n.strip()]
                for n in names:
                    if not re.fullmatch(r"[A-Za-z_]\w*", n):
                        raise ParseError(line_no, f"некорректное имя переменной: {n}")
                body.append(Input(names))
                continue

            # PRINT
            m = re.match(r"^PRINT\s+(.+)$", line, re.IGNORECASE)
            if m:
                next_tok()
                exprs = [e for e in split_top_level(m.group(1)) if e]
                body.append(Print(exprs))
                continue

            # IF ... THEN
            m = re.match(r"^IF\s+(.+?)\s+THEN$", line, re.IGNORECASE)
            if m:
                next_tok()
                cond = m.group(1).strip()
                then_body = parse_block({"ELSE", "ENDIF"})
                else_body: list[Node] = []
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
                var, a, b = m.group(1), m.group(2).strip(), m.group(3).strip()
                inner = parse_block({"ENDFOR"})
                t2 = peek()
                if not t2 or t2[1].upper() != "ENDFOR":
                    raise ParseError(line_no, "не найден ENDFOR")
                next_tok()
                body.append(For(var, a, b, inner))
                continue

            # WHILE cond
            m = re.match(r"^WHILE\s+(.+)$", line, re.IGNORECASE)
            if m:
                next_tok()
                cond = m.group(1).strip()
                inner = parse_block({"ENDWHILE"})
                t2 = peek()
                if not t2 or t2[1].upper() != "ENDWHILE":
                    raise ParseError(line_no, "не найден ENDWHILE")
                next_tok()
                body.append(While(cond, inner))
                continue

            # присваивание
            m = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.+)$", line)
            if m:
                next_tok()
                body.append(Assign(m.group(1), m.group(2).strip()))
                continue

            raise ParseError(line_no, f"не понимаю команду: {line!r}")

    ast = parse_block(set())
    if not ast:
        raise ParseError(0, "пустая схема")
    return ast


# ============================================================
#             2. ТРАНСФОРМАЦИЯ ВЫРАЖЕНИЙ ПОД ЯЗЫК
# ============================================================

def translate_expr(expr: str, lang: str) -> str:
    """
    Приводит выражение к синтаксису целевого языка.
    Логика:
      - True/False
      - != / ==  (одинаково)
      - AND/OR/NOT -> &&/||/! (для C++/Java), And/Or/Not (VB), and/or/not (Python)
      - строки "..." оставляем как есть
    """
    s = expr
    # маскируем строки, чтобы не трогать их содержимое
    strings: list[str] = []

    def mask(m):
        strings.append(m.group(0))
        return f"\x00{len(strings) - 1}\x00"

    s = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', mask, s)

    # логические операторы по словам
    s = re.sub(r"\bAND\b", "__AND__", s, flags=re.IGNORECASE)
    s = re.sub(r"\bOR\b", "__OR__", s, flags=re.IGNORECASE)
    s = re.sub(r"\bNOT\b", "__NOT__", s, flags=re.IGNORECASE)
    s = re.sub(r"\bTRUE\b", "__TRUE__", s, flags=re.IGNORECASE)
    s = re.sub(r"\bFALSE\b", "__FALSE__", s, flags=re.IGNORECASE)

    if lang == "python":
        s = s.replace("__AND__", "and").replace("__OR__", "or").replace("__NOT__", "not")
        s = s.replace("__TRUE__", "True").replace("__FALSE__", "False")
    elif lang in ("cpp", "java"):
        s = s.replace("__AND__", "&&").replace("__OR__", "||").replace("__NOT__", "!")
        s = s.replace("__TRUE__", "true").replace("__FALSE__", "false")
        # строки в C++/Java оставляем как есть
    elif lang == "vb":
        s = s.replace("__AND__", "And").replace("__OR__", "Or").replace("__NOT__", "Not")
        s = s.replace("__TRUE__", "True").replace("__FALSE__", "False")

    # вернуть строки обратно
    def unmask(m):
        return strings[int(m.group(1))]

    s = re.sub(r"\x00(\d+)\x00", unmask, s)
    return s


def is_string_literal(expr: str) -> bool:
    e = expr.strip()
    return len(e) >= 2 and e[0] == '"' and e[-1] == '"'


# ============================================================
#                   3. ГЕНЕРАТОРЫ КОДА
# ============================================================

class CodeGenBase:
    def __init__(self):
        self.lines: list[str] = []
        self.indent = 0

    def w(self, text: str = "") -> None:
        self.lines.append(("    " * self.indent) + text if text else "")

    def gen(self, ast: list[Node]) -> str:
        for n in ast:
            self.node(n)
        return "\n".join(self.lines)

    def node(self, n: Node) -> None:
        raise NotImplementedError


class PythonGen(CodeGenBase):
    def gen(self, ast):
        # собираем, потом добавим заголовок
        for n in ast:
            self.node(n)
        body = "\n".join(self.lines)
        header = (
            "# Автоматически сгенерировано из схемы алгоритма\n"
            "def main():\n"
        )
        body_ind = "\n".join("    " + l if l else "" for l in self.lines)
        return header + (body_ind if body_ind.strip() else "    pass") + '\n\n\nif __name__ == "__main__":\n    main()\n'

    def node(self, n: Node) -> None:
        if isinstance(n, Input):
            for name in n.names:
                self.w(f'{name} = input("Введите {name}: ")')
        elif isinstance(n, Assign):
            self.w(f"{n.name} = {translate_expr(n.expr, 'python')}")
        elif isinstance(n, Print):
            parts = []
            for e in n.exprs:
                if is_string_literal(e):
                    parts.append(e)
                else:
                    parts.append(f"str({translate_expr(e, 'python')})")
            self.w("print(" + ", ".join(parts) + ")")
        elif isinstance(n, If):
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
        elif isinstance(n, For):
            a = translate_expr(n.start, "python")
            b = translate_expr(n.end, "python")
            self.w(f"for {n.var} in range({a}, {b} + 1):")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
        elif isinstance(n, While):
            self.w(f"while {translate_expr(n.cond, 'python')}:")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1


class JavaGen(CodeGenBase):
    def gen(self, ast):
        # тело main
        body_lines: list[str] = []
        old = self.lines
        self.lines = body_lines
        for n in ast:
            self.node(n)
        self.lines = old

        inner = "\n".join("        " + l if l else "" for l in body_lines)
        return (
            "// Автоматически сгенерировано из схемы алгоритма\n"
            "import java.util.Scanner;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            "        Scanner sc = new Scanner(System.in);\n"
            f"{inner}\n"
            "        sc.close();\n"
            "    }\n"
            "}\n"
        )

    def node(self, n: Node) -> None:
        if isinstance(n, Input):
            for name in n.names:
                self.w(f'System.out.print("Введите {name}: ");')
                self.w(f'{name} = sc.nextLine();')  # как строка; можно поправить под тип
        elif isinstance(n, Assign):
            self.w(f"{n.name} = {translate_expr(n.expr, 'java')};")
        elif isinstance(n, Print):
            parts = []
            for e in n.exprs:
                if is_string_literal(e):
                    parts.append(e)
                else:
                    parts.append(f"{translate_expr(e, 'java')}")
            self.w("System.out.println(" + " + \" \" + ".join(parts) + ");")
        elif isinstance(n, If):
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
        elif isinstance(n, For):
            a = translate_expr(n.start, "java")
            b = translate_expr(n.end, "java")
            self.w(f"for (int {n.var} = {a}; {n.var} <= {b}; {n.var}++) {{")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("}")
        elif isinstance(n, While):
            self.w(f"while ({translate_expr(n.cond, 'java')}) {{")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("}")


class CppGen(CodeGenBase):
    def gen(self, ast):
        body_lines: list[str] = []
        old = self.lines
        self.lines = body_lines
        for n in ast:
            self.node(n)
        self.lines = old

        inner = "\n".join("    " + l if l else "" for l in body_lines)
        return (
            "// Автоматически сгенерировано из схемы алгоритма\n"
            "#include <iostream>\n"
            "#include <string>\n"
            "using namespace std;\n\n"
            "int main() {\n"
            f"{inner}\n"
            "    return 0;\n"
            "}\n"
        )

    def node(self, n: Node) -> None:
        if isinstance(n, Input):
            for name in n.names:
                self.w(f'cout << "Введите {name}: ";')
                self.w(f"cin >> {name};")
        elif isinstance(n, Assign):
            self.w(f"{n.name} = {translate_expr(n.expr, 'cpp')};")
        elif isinstance(n, Print):
            parts = []
            for e in n.exprs:
                if is_string_literal(e):
                    parts.append(e)
                else:
                    parts.append(translate_expr(e, "cpp"))
            self.w("cout << " + " << \" \" << ".join(parts) + " << endl;")
        elif isinstance(n, If):
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
        elif isinstance(n, For):
            a = translate_expr(n.start, "cpp")
            b = translate_expr(n.end, "cpp")
            self.w(f"for (int {n.var} = {a}; {n.var} <= {b}; {n.var}++) {{")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("}")
        elif isinstance(n, While):
            self.w(f"while ({translate_expr(n.cond, 'cpp')}) {{")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("}")


class VbGen(CodeGenBase):
    def gen(self, ast):
        self.w("' Автоматически сгенерировано из схемы алгоритма")
        self.w("Module Program")
        self.indent += 1
        self.w("Sub Main()")
        self.indent += 1
        for n in ast:
            self.node(n)
        self.indent -= 1
        self.w("End Sub")
        self.indent -= 1
        self.w("End Module")
        return "\n".join(self.lines)

    def node(self, n: Node) -> None:
        if isinstance(n, Input):
            for name in n.names:
                self.w(f'Console.Write("Введите {name}: ")')
                self.w(f"{name} = Console.ReadLine()")
        elif isinstance(n, Assign):
            self.w(f"{n.name} = {translate_expr(n.expr, 'vb')}")
        elif isinstance(n, Print):
            parts = []
            for e in n.exprs:
                if is_string_literal(e):
                    parts.append(e)
                else:
                    parts.append(f"CStr({translate_expr(e, 'vb')})")
            self.w("Console.WriteLine(" + " & \" \" & ".join(parts) + ")")
        elif isinstance(n, If):
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
        elif isinstance(n, For):
            a = translate_expr(n.start, "vb")
            b = translate_expr(n.end, "vb")
            self.w(f"For {n.var} = {a} To {b}")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("Next")
        elif isinstance(n, While):
            self.w(f"While {translate_expr(n.cond, 'vb')}")
            self.indent += 1
            for x in n.body:
                self.node(x)
            self.indent -= 1
            self.w("End While")


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
#                       4. БОТ
# ============================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
user_lang: dict[int, str] = {}


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
    # telegram имеет лимит 4096 — режем
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
