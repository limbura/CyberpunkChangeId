#!/usr/bin/env python3
"""CYBERWALL — смена ID. Python 3.11+, Windows / POSIX terminal.

Запуск: python change_id.py [--config change_id.toml] [--debug]
Соседние репозитории: CyberpunkPcRfidReader, CyberpunkIdPrinter, TiMini-Print.
Настройки необязательны; все относительные пути считаются от папки скрипта.
change_id_state.sqlite3 — обязательная контрольная точка, НЕ отладочный лог.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import tomllib
import warnings
from dataclasses import asdict, dataclass, replace
from enum import Enum

BASE = Path(__file__).resolve().parent
LOG = logging.getLogger("change_id")
DEFAULTS = {
    "database": {"host": "188.134.70.190", "port": 3306, "user": "master",
                 "name": "cyberwall", "password": "master", "connect_timeout": 5,
                 "read_timeout": 15, "write_timeout": 15, "attempts": 3},
    "reader": {"library_path": "../CyberpunkPcRfidReader/wcopy_reader_package",
               "timeout_ms": 750, "poll_interval": 0.25, "attempts": 5},
    "printer": {"library_path": "../CyberpunkIdPrinter/src",
                "timini_path": "../TiMini-Print", "address": "EB:11:06:26:77:26",
                "timeout": 20.0, "scan_timeout": 15.0, "connect_attempts": 2,
                "reconnect_delay": 5.0, "connection_check_interval": 2.0,
                "mode": "id-name", "roll_width_mm": 40.0,
                "width_mm": 40.0, "height_mm": 30.0, "offset_x_mm": 0.0,
                "feed_mm": 15.0, "retract_mm": 25.0,
                "position_timeout": 20.0, "settle_seconds": 3.0,
                "motion_settle_seconds": 5.0},
    "logging": {"enabled": False},
}
FAULTS = {"paper_out": "Нет бумаги или открыта крышка принтера",
          "overheated": "Принтер перегрелся",
          "low_battery": "Низкий заряд батареи принтера"}


class Stage(str, Enum):
    IDLE = "idle"
    WAIT_CARD = "wait_card"
    LOOKUP = "lookup"
    CONFIRM = "confirm"
    CHANGE = "change"
    PRINT = "print"
    PRINTING = "printing"
    LABEL = "label"
    RETRY = "retry"
    REMOVE = "remove"
    MANUAL = "manual"
    VERIFY = "verify"
    FINISH = "finish"


PRE_CALL = {Stage.IDLE, Stage.WAIT_CARD, Stage.LOOKUP, Stage.CONFIRM}


@dataclass(frozen=True)
class Operation:
    stage: Stage = Stage.IDLE
    card_id: int = 0
    name: str = ""
    old_id: int = 0
    new_id: int = 0
    db_attempts: int = 0
    input_errors: int = 0
    notice: str = ""
    result: str = ""
    database: str = ""

    @property
    def locked(self):
        return self.stage not in PRE_CALL


def normalize_id(text):
    value = "".join(text.split())
    return value if re.fullmatch(r"[0-9]{9}", value) else None


def formatted_id(number):
    s = str(number)
    return " ".join(s[i:i + 3] for i in range(0, len(s), 3))


def safe_name(name):
    # Never render control characters obtained from an account name.
    text = str(name or "Без имени")
    if text.isspace():
        text = "Без имени"
    return "".join(c for c in text if c.isprintable())


class StateError(Exception):
    pass


class Store:
    """One durable snapshot, committed before external side effects.

    SQLite rollback journal + synchronous=FULL protects the previous snapshot
    against process termination. This is not a history or a technical log.
    """
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        self.db.commit()

    def load(self):
        row = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        if row is None:
            return Operation()
        try:
            data = json.loads(row[0])
            if data.pop("version") != 1:
                raise ValueError("state version")
            data["stage"] = Stage(data["stage"])
            op = Operation(**data)
            for key in ("card_id", "old_id", "new_id", "db_attempts", "input_errors"):
                if type(getattr(op, key)) is not int or getattr(op, key) < 0:
                    raise ValueError(key)
            if op.stage not in {Stage.IDLE, Stage.WAIT_CARD, Stage.FINISH} and not op.card_id:
                raise ValueError("missing card")
            if op.stage in {Stage.PRINT, Stage.PRINTING, Stage.LABEL, Stage.RETRY,
                            Stage.REMOVE, Stage.MANUAL, Stage.VERIFY}:
                if not 100000000 <= op.new_id <= 999999999:
                    raise ValueError("missing confirmed ID")
            return op
        except (ValueError, TypeError, KeyError) as exc:
            raise StateError("Повреждено сохранённое состояние. Позови мастера; файл не удалён.") from exc

    def save(self, op):
        try:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)",
                                (json.dumps({"version": 1, **asdict(op)}, ensure_ascii=False),))
        except sqlite3.Error as exc:
            raise StateError("Не удалось сохранить этап. Работа остановлена; позови мастера.") from exc

    def close(self):
        self.db.close()


class InstanceLock:
    """OS releases the lock even after kill; the file itself may remain."""
    def __enter__(self):
        self.file = (BASE / "change_id.lock").open("a+b")
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise StateError("Другой экземпляр change_id.py уже работает.") from exc
        return self

    def __exit__(self, *exc):
        self.file.close()


class Cancel(Exception):
    pass


class Console:
    """Nonblocking keyboard input; no background input() consuming next screen.

    Prompts discard keys accumulated while DB/BLE was working. Empty Enter never
    chooses a menu item. On Windows extended keys are consumed as whole pairs.
    """
    def __init__(self):
        self.cancel_requested = False
        self.extended = False

    def drain(self):
        self.cancel_requested = False
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getwch()
            self.extended = False
        elif sys.stdin.isatty():
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)

    def poll(self):
        if os.name == "nt":
            import msvcrt
            if not msvcrt.kbhit():
                return None
            c = msvcrt.getwch()
            if self.extended:
                self.extended = False
                return None
            if c in ("\0", "\xe0"):
                self.extended = True
                return None
            return c
        import select
        if select.select([sys.stdin], [], [], 0)[0]:
            c = sys.stdin.readline()
            if not c:
                raise EOFError
            return c
        return None

    async def ask(self, text, *, choices=None, cancellable=False):
        self.drain()
        print(text, flush=True)
        if cancellable:
            print(":b — отмена | Enter — подтвердить ввод")
        else:
            print("Enter — подтвердить ввод")
        buffer = ""
        while True:
            if self.cancel_requested:
                self.cancel_requested = False
                if cancellable:
                    raise Cancel
            c = self.poll()
            if c is None:
                await asyncio.sleep(0.04)
                continue
            if os.name == "nt":
                if c in ("\x03", "\x1b"):
                    if cancellable:
                        raise Cancel
                    continue
                if c == "\b":
                    if buffer:
                        buffer = buffer[:-1]
                        print("\b \b", end="", flush=True)
                    continue
                if c not in ("\r", "\n"):
                    if c.isprintable() and len(buffer) < 100:
                        buffer += c
                        print(c, end="", flush=True)
                    continue
                print()
                value, buffer = buffer.strip(), ""
            else:
                value = c.strip()
            if value.casefold() in (":b", ":q"):
                if cancellable:
                    raise Cancel
                print("Смена ID уже начата. Сначала заверши замену наклейки.")
                continue
            if not value:  # Repeated Enter cannot advance the next screen.
                continue
            if choices is None or value.casefold() in choices:
                return value.casefold() if choices else value
            print("Выбери один из указанных вариантов.")

    async def acknowledge(self, text, *, cancellable=False):
        return await self.ask(text + "\n1 — понятно, продолжить", choices={"1"}, cancellable=cancellable)


class Database:
    def __init__(self, config):
        self.config = config
        self.password = os.environ.get("CYBERWALL_DB_PASSWORD", config["password"])
        self.password_requested = bool(self.password)

    def ensure_password(self):
        if not self.password_requested:
            self.password = getpass.getpass("Пароль БД (gate): ")
            self.password_requested = True

    def connection(self):
        import pymysql
        c = self.config
        return pymysql.connect(host=c["host"], port=c["port"], user=c["user"],
                               password=self.password, database=c["name"], charset="utf8mb4",
                               autocommit=True, connect_timeout=c["connect_timeout"],
                               read_timeout=c["read_timeout"], write_timeout=c["write_timeout"],
                               cursorclass=pymysql.cursors.DictCursor)

    def lookup(self, card_id):
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT id, name FROM main WHERE card_id=%s", (card_id,))
            return cursor.fetchone()

    def change(self, card_id):
        # A fresh connection for each attempt: never silently replay a command
        # inside a transport helper. changeId owns its MySQL transaction.
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SET @gate_new_id=NULL, @gate_err=NULL")
            cursor.execute("CALL changeId(%s, @gate_new_id, @gate_err)", (card_id,))
            while cursor.nextset():
                pass
            cursor.execute("SELECT @gate_new_id AS new_id, @gate_err AS code")
            row = cursor.fetchone()
        if row and row["code"] == 1:
            raise AccountMissing
        if not row or row["code"] != 0 or not row["new_id"]:
            raise RuntimeError("Invalid changeId response")
        number = int(row["new_id"])
        if not 100000000 <= number <= 999999999:
            raise RuntimeError("ID out of range")
        return number


class AccountMissing(Exception):
    pass


class Reader:
    def __init__(self, config):
        self.config = config

    async def wait(self, ui):
        module = importlib.import_module("wcopy_reader")
        readers = await asyncio.to_thread(module.list_readers)
        if len(readers) != 1:
            raise RuntimeError("Exactly one reader required")
        path = readers[0].path
        errors = 0
        feedback = True
        ui.drain()
        print("Приложи карту пользователя. Esc / Ctrl+C — отмена (в Linux: :b и Enter).")
        while True:
            c = ui.poll()
            if ui.cancel_requested or (c is not None and c.strip() in ("\x1b", "\x03", ":b", ":q")):
                raise Cancel
            try:
                number = await asyncio.to_thread(module.read_card_number, path=path,
                                                timeout_ms=self.config["timeout_ms"])
                present = True
            except module.NoCardError:
                number, present = None, False
            except module.RFIDError as exc:
                LOG.debug("RFID failure", exc_info=True)
                if exc.code in {"READER_NOT_FOUND", "MULTIPLE_READERS", "READER_OPEN_ERROR",
                                "USB_ERROR", "DEPENDENCY_ERROR"}:
                    raise
                errors += 1
                if errors >= self.config["attempts"]:
                    raise
                print("Не удалось прочитать карту. Удерживай её на считывателе…")
                await asyncio.sleep(0.5)
                continue
            if feedback:
                try:
                    await asyncio.to_thread(module.set_led, "green" if present else "red",
                                            path=path, timeout_ms=self.config["timeout_ms"])
                except module.RFIDError:
                    LOG.debug("RFID feedback disabled", exc_info=True)
                    feedback = False
            # A cancellation received during a blocking HID call wins before DB.
            if ui.cancel_requested:
                raise Cancel
            if number is not None:
                return number
            await asyncio.sleep(self.config["poll_interval"])


class Printer:
    def __init__(self, config):
        self.config = config
        self.device = None
        self.removed = False
        self._io = asyncio.Lock()
        self._attempt = None
        self._monitor = None
        self._foreground = 0

    def start(self):
        """Start monitoring without delaying the menu or moving paper."""
        if self._monitor is None:
            self._monitor = asyncio.create_task(self._watch())

    async def _watch(self):
        while True:
            if self._foreground:
                await asyncio.sleep(0.1)
                continue
            try:
                await self._shared_attempt()
                delay = self.config["connection_check_interval"]
            except Exception:
                LOG.debug("Background printer connection failed", exc_info=True)
                delay = self.config["reconnect_delay"]
            await asyncio.sleep(delay)

    async def _shared_attempt(self):
        # Foreground waits for an already running attempt; never a second BLE job.
        if self._attempt is None or self._attempt.done():
            self._attempt = asyncio.create_task(self._connect_once())
        await asyncio.shield(self._attempt)

    def create(self):
        from cyberwall_id_printer import LabelPrinter, LabelSettings, MediaConfig, PaperType
        c = self.config
        self.device = LabelPrinter(
            timini_path=str((BASE / c["timini_path"]).resolve()),
            media=MediaConfig(paper_type=PaperType.LABELS,
                              **{k: c[k] for k in ("width_mm", "height_mm", "roll_width_mm", "offset_x_mm")}),
            settings=LabelSettings(**{k: c[k] for k in (
                "feed_mm", "retract_mm", "position_timeout", "settle_seconds", "motion_settle_seconds")}),
            timeout=c["timeout"], scan_timeout=c["scan_timeout"],
            connect_attempts=1,  # Retry budget belongs to this service, not the backend.
            on_event=lambda event: LOG.debug("printer %s", event))

    async def _connect_once(self):
        async with self._io:
            if self.device is None:
                self.create()
            if await self.device.check_connection():
                return
            await self.device.disconnect()
            LOG.debug("Printer connection attempt")
            await self.device.connect(self.config["address"])
            self.removed = False

    async def connect(self):
        """Wait for at most N attempts from now, including one already in flight.

        Earlier background failures do not consume the print-stage budget.
        The monitor remains active after this method fails (manual mode).
        """
        self._foreground += 1
        try:
            for attempt in range(self.config["connect_attempts"]):
                try:
                    await self._shared_attempt()
                    return
                except Exception:
                    if attempt + 1 == self.config["connect_attempts"]:
                        raise
                    await asyncio.sleep(self.config["reconnect_delay"])
        finally:
            self._foreground -= 1

    async def alive(self):
        async with self._io:
            return self.device is not None and await self.device.check_connection()

    async def print_id(self, number, name):
        from cyberwall_id_printer import (
            LabelMode, UnsupportedNameCharactersWarning, NameTruncatedWarning, InvalidNameWarning,
        )
        # No background status query/reconnect during paper motion or printing.
        async with self._io:
            if self.device.state.value == "presented" and self.removed:
                await self.device.confirm_removed()
            self.removed = False
            messages = {
                NameTruncatedWarning: "Слишком длинное имя обрезано",
                InvalidNameWarning: "Некорректное или пустое имя, напиши имя на наклейке вручную и сообщи мастерам",
            }
            caught = []
            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("ignore", UnsupportedNameCharactersWarning)
                    for category in messages:
                        warnings.simplefilter("always", category)
                    await self.device.print_id(str(number), name, mode=LabelMode(self.config["mode"]))
            finally:
                displayed = set()
                for warning in caught:
                    category = next((kind for kind in messages
                                     if issubclass(warning.category, kind)), None)
                    if category is None:
                        warnings.showwarning(warning.message, warning.category,
                                             warning.filename, warning.lineno,
                                             line=warning.line)
                    elif category not in displayed:
                        print(messages[category])
                        displayed.add(category)

    async def close(self):
        tasks = [task for task in (self._monitor, self._attempt) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._io:
            if self.device is not None:
                try:
                    await self.device.disconnect()
                except Exception:
                    LOG.debug("Disconnect failed", exc_info=True)


class App:
    def __init__(self, config, store, ui, db, reader, printer):
        self.config, self.store, self.ui = config, store, ui
        self.db, self.reader, self.printer = db, reader, printer
        self.op = store.load()
        c = config["database"]
        self.database_key = json.dumps([c["host"], c["port"], c["name"]])
        if self.op.stage != Stage.IDLE and self.op.database != self.database_key:
            raise StateError("Незавершённая операция относится к другой БД. Верни прежние настройки подключения.")
        if self.op.stage == Stage.PRINTING:
            self.go(Stage.LABEL, notice="Программа закрылась во время печати. Проверь, вышла ли нужная наклейка.")

    def go(self, stage, **updates):
        candidate = replace(self.op, stage=stage, **updates)
        self.store.save(candidate)  # Failure must stop, never execute next side effect.
        self.op = candidate
        LOG.debug("stage=%s card_id=%s new_id=%s", stage.value, candidate.card_id, candidate.new_id)

    def finish(self, message):
        self.go(Stage.FINISH, result=message)

    def reset(self):
        candidate = Operation()
        self.store.save(candidate)
        self.op = candidate

    def signal(self, *_):
        if not self.op.locked:
            self.ui.cancel_requested = True
        # No exception inside DB calls, SQLite commits or mechanical operations.

    def header(self):
        print("\n" + "═" * 54)
        print("CYBERWALL — СМЕНА ID")
        if self.op.name:
            print("Аккаунт:", self.op.name)
        if self.op.new_id:
            print("НОВЫЙ ID:", formatted_id(self.op.new_id))
        print("═" * 54)

    async def step(self):
        s = self.op.stage
        if s == Stage.IDLE:
            choice = await self.ui.ask("1 — сменить ID\n0 — выйти", choices={"1", "0"}, cancellable=True)
            if choice == "0":
                return False
            self.go(Stage.WAIT_CARD, database=self.database_key)
        elif s == Stage.WAIT_CARD:
            try:
                card = await self.reader.wait(self.ui)
            except Cancel:
                raise
            except Exception:
                LOG.debug("Reader unavailable", exc_info=True)
                self.finish("Не удалось прочитать карту или считыватель недоступен. Попробуйте другой гейт; сообщи мастерам.")
            else:
                if card == 0:
                    self.finish("Карта не записана. Отправь владельца карты на мастерку.")
                else:
                    self.go(Stage.LOOKUP, card_id=card)
        elif s == Stage.LOOKUP:
            self.db.ensure_password()
            try:
                row = await asyncio.to_thread(self.db.lookup, self.op.card_id)
            except Exception:
                LOG.debug("Lookup failed", exc_info=True)
                await self.ui.acknowledge("БД недоступна. ID не менялся. Попробуй позже.", cancellable=True)
                self.reset()
            else:
                if not row:
                    self.finish("Аккаунт не найден. Отправь владельца карты на мастерку.")
                else:
                    self.go(Stage.CONFIRM, name=safe_name(row["name"]), old_id=int(row["id"]))
        elif s == Stage.CONFIRM:
            choice = await self.ui.ask(
                "Сейчас ID будет изменён. После этого обязательно замени наклейку.\n"
                "Если закончить не получится — отправь владельца карты на мастерку.\n"
                "1 — начать смену ID\n2 — отмена", choices={"1", "2"}, cancellable=True)
            if choice == "2":
                self.reset()
            else:
                self.go(Stage.CHANGE)  # Commit intent immediately BEFORE CALL.
        elif s == Stage.CHANGE:
            self.db.ensure_password()
            if self.op.db_attempts >= self.config["database"]["attempts"]:
                self.finish("Не удалось подтвердить смену ID. ID мог измениться. Отправь владельца карты на мастерку.")
                return True
            self.go(Stage.CHANGE, db_attempts=self.op.db_attempts + 1)
            print("Меняю ID…", flush=True)
            try:
                number = await asyncio.to_thread(self.db.change, self.op.card_id)
            except AccountMissing:
                self.finish("Аккаунт не найден. Отправь владельца карты на мастерку.")
            except Exception:
                LOG.debug("changeId result unconfirmed", exc_info=True)
                print("Результат не подтверждён. Повторяю подключение и смену ID…")
                await asyncio.sleep(0.5)
            else:
                self.go(Stage.PRINT, new_id=number)
        elif s == Stage.PRINT:
            try:
                print("Проверяю подключение к термопринтеру…", flush=True)
                await self.printer.connect()
            except Exception:
                LOG.debug("Printer connection failed", exc_info=True)
                self.go(Stage.MANUAL, notice="Не удалось подключиться к принтеру")
            else:
                # Store before transmission. On restart ask about physical output.
                self.go(Stage.PRINTING)
                try:
                    print("Печатаю наклейку...", flush=True)
                    await self.printer.print_id(self.op.new_id, self.op.name)
                except Exception as exc:
                    await self.print_failure(exc)
                else:
                    self.go(Stage.LABEL, notice="")
        elif s == Stage.LABEL:
            print(self.op.notice)
            choice = await self.ui.ask(
                "Наклейка напечаталась, и ты наклеил её на карту ВЗАМЕН старой?\n"
                "1 — да, заменил\n2 — нет, попробовать напечатать ещё раз\n"
                "3 — нет, заполнить вручную", choices={"1", "2", "3"})
            if choice == "1":
                self.printer.removed = True
                self.finish("Смена ID завершена.")
            elif choice == "2":
                self.go(Stage.REMOVE)
            else:
                self.go(Stage.MANUAL, notice="Не удалось получить наклейку")
        elif s == Stage.RETRY:
            if self.op.notice:
                print(self.op.notice)
            choice = await self.ui.ask("1 — попробовать печать ещё раз\n2 — заполнить вручную", choices={"1", "2"})
            self.go(Stage.REMOVE if choice == "1" else Stage.MANUAL)
        elif s == Stage.REMOVE:
            await self.ui.acknowledge("Отклей от вышедшей ленты все доступные наклейки и выброси их.")
            self.printer.removed = True
            self.go(Stage.PRINT)
        elif s == Stage.MANUAL:
            print(self.op.notice + ". После смены ID сообщи об этом мастерам.")
            await self.ui.acknowledge(
                "Возьми пустой шаблон и маркер. Наклей шаблон ВЗАМЕН старой наклейки.\n"
                "Напиши ID: " + formatted_id(self.op.new_id) + "\nНажми 1, когда закончишь.")
            self.go(Stage.VERIFY)
        elif s == Stage.VERIFY:
            value = await self.ui.ask("Введи ID, который ты написал на наклейке:")
            if normalize_id(value) == str(self.op.new_id):
                self.finish("Смена ID завершена. Сообщи мастерам: " + self.op.notice + ".")
            else:
                count = self.op.input_errors + 1
                if count >= 5:
                    self.finish("ID пять раз введён неверно. Идите с владельцем карты на мастерку.")
                else:
                    self.go(Stage.VERIFY, input_errors=count)
                    print("\nНеверный ID. Исправь наклейку и введи снова. Должно быть:", formatted_id(self.op.new_id))
        elif s == Stage.FINISH:
            # A crash on this screen must repeat the instruction, not silently
            # discard an unresolved handoff to the game masters.
            await self.ui.acknowledge(self.op.result)
            self.reset()
        else:
            raise StateError("Неизвестный этап операции.")
        return True

    async def print_failure(self, exc):
        LOG.debug("Print failed", exc_info=(type(exc), exc, exc.__traceback__))
        reason = getattr(exc, "reason", None)
        if reason in FAULTS:
            self.go(Stage.MANUAL, notice=FAULTS[reason])
            return
        if type(exc).__name__ == "RemovalRequired":
            self.go(Stage.REMOVE)
            return
        try:
            alive = await self.printer.alive()
        except Exception:
            alive = False
        if not alive:
            print("Связь с принтером потеряна. Пробую переподключиться…")
            try:
                await self.printer.connect()
            except Exception:
                LOG.debug("Reconnect failed", exc_info=True)
                self.go(Stage.MANUAL, notice="Не удалось восстановить связь с принтером")
                return
            # Reconnect does not automatically duplicate a possibly printed job.
            self.go(Stage.LABEL, notice="Связь восстановлена. Проверь результат прерванной печати.")
        else:
            self.go(Stage.RETRY, notice="Ошибка печати")

    async def run(self):
        signal.signal(signal.SIGINT, self.signal)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self.signal)
        if self.op.stage != Stage.IDLE:
            print("Восстанавливаю незавершённую смену ID.")
        while True:
            self.header()
            try:
                if self.ui.cancel_requested and not self.op.locked:
                    raise Cancel
                if not await self.step():
                    break
            except Cancel:
                if self.op.locked:
                    continue
                if self.op.stage == Stage.IDLE:
                    break
                self.reset()
                print("Смена ID отменена.")


def load_config(path):
    config = {key: dict(value) for key, value in DEFAULTS.items()}
    if path.exists():
        with path.open("rb") as f:
            overrides = tomllib.load(f)
        for section, values in overrides.items():
            if section not in config or not isinstance(values, dict):
                raise ValueError("Unknown config section: " + section)
            unknown = values.keys() - config[section].keys()
            if unknown:
                raise ValueError("Unknown config keys: " + str(unknown))
            config[section].update(values)
    # if config["database"]["user"] != "gate":
    #     raise ValueError("Database user must be gate")
    for section, keys in {"database": ("attempts", "connect_timeout", "read_timeout", "write_timeout"),
                          "reader": ("attempts", "timeout_ms")}.items():
        for key in keys:
            if type(config[section][key]) is not int or config[section][key] <= 0:
                raise ValueError(section + "." + key)
    for key in ("timeout", "scan_timeout", "reconnect_delay", "connection_check_interval", "width_mm", "height_mm", "roll_width_mm",
                "feed_mm", "retract_mm", "position_timeout", "settle_seconds", "motion_settle_seconds"):
        value = config["printer"][key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("printer." + key)
    p = config["printer"]
    if p["mode"] not in {"id-name", "id-only"}:
        raise ValueError("Only populated label modes are allowed")
    if p["width_mm"] > p["roll_width_mm"] or min(p["settle_seconds"], p["motion_settle_seconds"]) < 2:
        raise ValueError("Invalid paper or settling settings")
    if not all(0.25 <= p[k] <= 25 for k in ("feed_mm", "retract_mm")):
        raise ValueError("Invalid feed/retract distance")
    if type(p["connect_attempts"]) is not int or p["connect_attempts"] < 1:
        raise ValueError("Invalid connection attempts")
    interval = config["reader"]["poll_interval"]
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("Invalid reader poll interval")
    return config


def setup_logging(enabled):
    root = logging.getLogger()
    root.handlers.clear()
    if enabled:
        logging.disable(logging.NOTSET)
        handler = RotatingFileHandler(BASE / "change_id_debug.log", maxBytes=2_000_000,
                                      backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
    else:
        root.addHandler(logging.NullHandler())
        logging.disable(logging.CRITICAL)


async def run(config):
    store = Store(BASE / "change_id_state.sqlite3")
    printer = Printer(config["printer"])
    try:
        app = App(config, store, Console(), Database(config["database"]), Reader(config["reader"]), printer)
        printer.start()
        await app.run()
    finally:
        await printer.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description="CYBERWALL — смена ID игроком")
    parser.add_argument("--config", type=Path, default=BASE / "change_id.toml")
    parser.add_argument("--debug", action="store_true", help="включить технический журнал")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        setup_logging(args.debug or config["logging"]["enabled"])
        for section in ("reader", "printer"):
            sys.path.insert(0, str((BASE / config[section]["library_path"]).resolve()))
        if not sys.stdin.isatty():
            print("Запусти скрипт в обычном терминале, без перенаправления ввода.")
            return 1
        with InstanceLock():
            asyncio.run(run(config))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nВвод прерван. Сохранённый этап будет восстановлен при запуске.")
        return 1
    except StateError as exc:
        LOG.debug("State failure", exc_info=True)
        print(str(exc))
        return 1
    except Exception:
        LOG.debug("Fatal failure", exc_info=True)
        print("Работа остановлена. Если смена ID была начата, отправь владельца карты на мастерку.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


