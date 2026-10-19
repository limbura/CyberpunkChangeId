"""Run: python -m unittest discover -s tests -v. No devices/network required."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import change_id as m


class FakeUI:
    cancel_requested = False

    def __init__(self, *answers):
        self.answers = list(answers)

    async def ask(self, *args, **kwargs):
        return self.answers.pop(0)

    async def acknowledge(self, *args, **kwargs):
        return await self.ask()


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = m.Store(self.path)
        self.addCleanup(self.store.close)
        self.config = {k: dict(v) for k, v in m.DEFAULTS.items()}
        self.db = Mock()
        self.db.change.return_value = 123456789
        self.db.lookup.return_value = {'id': 987654321, 'name': 'РОЗА'}
        self.printer = Mock()
        self.printer.connect = AsyncMock()
        self.printer.print_id = AsyncMock()
        self.printer.alive = AsyncMock(return_value=True)
        self.reader = Mock()
        self.reader.wait = AsyncMock(return_value=42)
        self.ui = FakeUI()
        self.app = m.App(self.config, self.store, self.ui, self.db, self.reader, self.printer)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def start(self, stage, **kw):
        data = dict(card_id=42, name='РОЗА', database=self.app.database_key)
        if stage not in m.PRE_CALL | {m.Stage.CHANGE}:
            data['new_id'] = 123456789
        self.app.go(stage, **(data | kw))

    def restart(self):
        return m.App(self.config, self.store, self.ui, self.db, self.reader, self.printer)

    async def test_full_automatic_flow(self):
        self.ui.answers = ['1', '1', '1', '1']
        for _ in range(8):
            await self.app.step()
        self.assertEqual(self.app.op.stage, m.Stage.IDLE)
        self.db.change.assert_called_once_with(42)
        self.printer.print_id.assert_awaited_once_with(123456789, 'РОЗА')

    async def test_change_intent_is_durable_before_call(self):
        self.start(m.Stage.CHANGE)
        def change(card):
            connection = m.Store(self.path)
            try:
                saved = connection.load()
            finally:
                connection.close()
            self.assertEqual(saved.stage, m.Stage.CHANGE)
            self.assertEqual(saved.db_attempts, 1)
            return 123456789
        self.db.change.side_effect = change
        await self.app.step()
        self.assertEqual(self.store.load().new_id, 123456789)

    async def test_unconfirmed_database_call_retries_after_restart(self):
        self.start(m.Stage.CHANGE)
        self.db.change.side_effect = [ConnectionError(), 123456789]
        await self.app.step()
        app = self.restart()
        await app.step()
        self.assertEqual(app.op.stage, m.Stage.PRINT)
        self.assertEqual(self.db.change.call_count, 2)

    async def test_retry_budget_survives_restart(self):
        self.start(m.Stage.CHANGE, db_attempts=3)
        app = self.restart()
        await app.step()
        self.db.change.assert_not_called()
        self.assertEqual(app.op.stage, m.Stage.FINISH)
        self.assertIn('мастерку', app.op.result)

    async def test_known_id_never_changed_again(self):
        self.start(m.Stage.PRINT)
        app = self.restart()
        await app.step()
        self.db.change.assert_not_called()
        self.assertEqual(app.op.stage, m.Stage.LABEL)

    async def test_interrupted_print_asks_result_without_sending(self):
        self.start(m.Stage.PRINTING)
        app = self.restart()
        self.assertEqual(app.op.stage, m.Stage.LABEL)
        self.ui.answers = ['1']
        await app.step()
        self.printer.print_id.assert_not_awaited()
        self.db.change.assert_not_called()

    async def test_reprint_uses_same_id(self):
        self.start(m.Stage.LABEL)
        self.ui.answers = ['2', '1']
        await self.app.step()
        await self.app.step()
        await self.app.step()
        self.printer.print_id.assert_awaited_once_with(123456789, 'РОЗА')
        self.db.change.assert_not_called()

    async def test_known_faults_go_directly_manual(self):
        for reason in m.FAULTS:
            self.start(m.Stage.PRINT)
            err = RuntimeError()
            err.reason = reason
            self.printer.print_id.side_effect = err
            await self.app.step()
            self.assertEqual(self.app.op.stage, m.Stage.MANUAL)
            self.assertEqual(self.app.op.notice, m.FAULTS[reason])
        self.printer.alive.assert_not_awaited()

    async def test_connection_failure_goes_manual(self):
        self.start(m.Stage.PRINT)
        self.printer.connect.side_effect = ConnectionError()
        await self.app.step()
        self.assertEqual(self.app.op.stage, m.Stage.MANUAL)
        self.printer.print_id.assert_not_awaited()

    async def test_disconnect_reconnect_does_not_replay(self):
        self.start(m.Stage.PRINT)
        self.printer.print_id.side_effect = ConnectionError()
        self.printer.alive.return_value = False
        await self.app.step()
        self.assertEqual(self.app.op.stage, m.Stage.LABEL)
        self.assertEqual(self.printer.connect.await_count, 2)
        self.assertEqual(self.printer.print_id.await_count, 1)

    async def test_failed_reconnect_goes_manual(self):
        self.start(m.Stage.PRINT)
        self.printer.print_id.side_effect = ConnectionError()
        self.printer.alive.return_value = False
        self.printer.connect.side_effect = [None, ConnectionError()]
        await self.app.step()
        self.assertEqual(self.app.op.stage, m.Stage.MANUAL)

    async def test_unknown_fault_offers_retry(self):
        self.start(m.Stage.PRINT)
        self.printer.print_id.side_effect = ValueError()
        await self.app.step()
        self.assertEqual(self.app.op.stage, m.Stage.RETRY)

    async def test_manual_recovery_preserves_id_and_attempts(self):
        self.start(m.Stage.VERIFY, input_errors=3, notice='Нет бумаги')
        app = self.restart()
        self.ui.answers = ['111111111', '123 456 789']
        await app.step()
        self.assertEqual(self.store.load().input_errors, 4)
        await app.step()
        self.assertEqual(app.op.stage, m.Stage.FINISH)
        self.assertIn('завершена', app.op.result)
        self.db.change.assert_not_called()

    async def test_five_wrong_inputs_handoff_then_next_player(self):
        self.start(m.Stage.VERIFY, input_errors=4)
        self.ui.answers = ['111111111', '1']
        await self.app.step()
        self.assertIn('мастерку', self.app.op.result)
        app = self.restart()
        await app.step()
        self.assertEqual(self.store.load().stage, m.Stage.IDLE)

    async def test_failure_to_save_intent_prevents_call(self):
        self.start(m.Stage.CONFIRM)
        self.ui.answers = ['1']
        with patch.object(self.store, 'save', side_effect=m.StateError()):
            with self.assertRaises(m.StateError):
                await self.app.step()
        self.db.change.assert_not_called()
        self.assertEqual(self.app.op.stage, m.Stage.CONFIRM)

    async def test_failure_to_save_print_intent_prevents_print(self):
        self.start(m.Stage.PRINT)
        with patch.object(self.store, 'save', side_effect=m.StateError()):
            with self.assertRaises(m.StateError):
                await self.app.step()
        self.printer.print_id.assert_not_awaited()

    async def test_crash_between_db_result_and_save_repeats_call(self):
        self.start(m.Stage.CHANGE)
        save = self.store.save
        def fail_result(op):
            if op.stage == m.Stage.PRINT:
                raise m.StateError()
            save(op)
        with patch.object(self.store, 'save', side_effect=fail_result):
            with self.assertRaises(m.StateError):
                await self.app.step()
        app = self.restart()
        await app.step()
        self.assertEqual(self.db.change.call_count, 2)

    async def test_missing_account_handoff(self):
        self.start(m.Stage.CHANGE)
        self.db.change.side_effect = m.AccountMissing()
        await self.app.step()
        self.assertEqual(self.app.op.stage, m.Stage.FINISH)

    async def test_cancel_confirmation_does_not_call_db(self):
        self.start(m.Stage.CONFIRM)
        self.ui.answers = ['2']
        await self.app.step()
        self.assertEqual(self.store.load().stage, m.Stage.IDLE)
        self.db.change.assert_not_called()

    def test_ctrl_c_after_call_is_ignored(self):
        self.start(m.Stage.CHANGE)
        self.app.signal()
        self.assertFalse(self.ui.cancel_requested)
        self.start(m.Stage.CONFIRM)
        self.app.signal()
        self.assertTrue(self.ui.cancel_requested)

    def test_database_change_during_operation_rejected(self):
        self.start(m.Stage.MANUAL)
        self.config['database']['host'] = 'another-host'
        with self.assertRaises(m.StateError):
            self.restart()


class StorageTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(m.normalize_id(' 123\t456\u00a0789 '), '123456789')
        for text in ('12345678', '1234567890', '１２３４５６７８９', '123-456-789', 'abc123456789'):
            self.assertIsNone(m.normalize_id(text))

    def test_actual_process_exit_preserves_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            code = "import os; from pathlib import Path; from change_id import *; s=Store(Path(__import__('sys').argv[1])); s.save(Operation(stage=Stage.CHANGE,card_id=42)); os._exit(23)"
            result = subprocess.run([sys.executable, '-c', code, str(path)], cwd=m.BASE)
            self.assertEqual(result.returncode, 23)
            store = m.Store(path)
            try:
                self.assertEqual(store.load().stage, m.Stage.CHANGE)
            finally:
                store.close()

    def test_corrupt_snapshot_is_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = m.Store(Path(directory) / 'state.sqlite3')
            try:
                store.db.execute("INSERT INTO state VALUES (1, 'bad-json')")
                store.db.commit()
                with self.assertRaises(m.StateError):
                    store.load()
                self.assertEqual(store.db.execute('SELECT payload FROM state').fetchone()[0], 'bad-json')
            finally:
                store.close()

    def test_second_process_cannot_start(self):
        code = "from change_id import InstanceLock; lock=InstanceLock(); lock.__enter__()"
        with m.InstanceLock():
            result = subprocess.run([sys.executable, '-c', code], cwd=m.BASE, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_logging_off_creates_no_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m, 'BASE', Path(directory)):
            m.setup_logging(False)
            m.LOG.error('should not be written')
            self.assertFalse(list(Path(directory).iterdir()))

    def test_blank_mode_config_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[printer]\nmode="blank-id-only"\n')
            with self.assertRaises(ValueError):
                m.load_config(path)


class InputAndReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_enter_does_not_confirm(self):
        ui = m.Console()
        with patch.object(ui, 'drain'), patch.object(ui, 'poll', side_effect=['\r', '\r', '1', '\r']), patch.object(m.os, 'name', 'nt'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await ui.ask('Choose', choices={'1'}), '1')

    async def test_exit_rejected_after_call(self):
        ui = m.Console()
        with patch.object(ui, 'drain'), patch.object(ui, 'poll', side_effect=[':q\n', '1\n']), patch.object(m.os, 'name', 'posix'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await ui.ask('Choose', choices={'1'}), '1')

    async def test_no_card_does_not_use_error_budget(self):
        class RFIDError(Exception):
            pass
        class NoCardError(RFIDError):
            pass
        module = Mock()
        module.RFIDError, module.NoCardError = RFIDError, NoCardError
        module.list_readers.return_value = [Mock(path=b'reader')]
        module.read_card_number.side_effect = [NoCardError(), NoCardError(), 42]
        module.set_led.side_effect = RFIDError()
        ui = Mock(cancel_requested=False)
        ui.poll.return_value = None
        reader = m.Reader({'timeout_ms': 750, 'attempts': 1, 'poll_interval': 0.001})
        with patch.object(m.importlib, 'import_module', return_value=module), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await reader.wait(ui), 42)
        self.assertEqual(module.read_card_number.call_count, 3)
        self.assertEqual(module.set_led.call_count, 1)


if __name__ == '__main__':
    unittest.main()


class PrinterServiceTests(unittest.IsolatedAsyncioTestCase):
    def make_printer(self):
        c = dict(m.DEFAULTS['printer'], reconnect_delay=0.001, connection_check_interval=0.001)
        p = m.Printer(c)
        self.addAsyncCleanup(p.close)
        return p

    async def test_start_connects_without_printing(self):
        p = self.make_printer()
        connected = asyncio.Event()
        async def once():
            connected.set()
        p._connect_once = AsyncMock(side_effect=once)
        p.start()
        await asyncio.wait_for(connected.wait(), 1)
        self.assertGreaterEqual(p._connect_once.await_count, 1)
        self.assertIsNone(p.device)

    async def test_background_retries_and_keeps_monitoring(self):
        p = self.make_printer()
        recovered = asyncio.Event()
        calls = 0
        async def once():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError()
            recovered.set()
        p._connect_once = once
        p.start()
        await asyncio.wait_for(recovered.wait(), 1)
        self.assertGreaterEqual(calls, 3)

    async def test_foreground_budget_and_shared_inflight_attempt(self):
        p = self.make_printer()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def once():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            raise ConnectionError()
        p._connect_once = once
        p.start()
        await entered.wait()
        foreground = asyncio.create_task(p.connect())
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(ConnectionError):
            await foreground
        self.assertEqual(calls, p.config['connect_attempts'])

    async def test_monitor_reconnects_on_lost_link(self):
        p = self.make_printer()
        p.device = Mock()
        p.device.check_connection = AsyncMock(side_effect=[True, False])
        p.device.disconnect = AsyncMock()
        p.device.connect = AsyncMock()
        await p._connect_once()
        p.device.connect.assert_not_awaited()
        await p._connect_once()
        p.device.connect.assert_awaited_once_with(p.config['address'])

    async def test_connection_waits_for_print_io_lock(self):
        p = self.make_printer()
        p.device = Mock()
        p.device.check_connection = AsyncMock(return_value=True)
        p.device.disconnect = AsyncMock()
        async with p._io:
            attempt = asyncio.create_task(p._shared_attempt())
            await asyncio.sleep(0.01)
            p.device.check_connection.assert_not_awaited()
        await attempt
        p.device.check_connection.assert_awaited_once()

    async def test_close_cancels_pending_attempt(self):
        p = self.make_printer()
        entered = asyncio.Event()
        async def once():
            entered.set()
            await asyncio.Event().wait()
        p._connect_once = once
        p.start()
        await entered.wait()
        await p.close()
        self.assertTrue(p._monitor.done())
        self.assertTrue(p._attempt.done())
