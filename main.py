"""Windows desktop entry point and headless maintenance commands."""
import argparse
import json
import os
from pathlib import Path
import sys


def show_startup_error(message, gui=False):
    if gui:
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, 'RubUP Automation — не удалось запустить', message)
            return
        except Exception:
            if os.name == 'nt':
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(None, message, 'RubUP Automation', 0x10)
                    return
                except Exception:
                    pass
    if sys.stderr is not None:
        print('Ошибка: ' + message, file=sys.stderr)


def main(argv=None):
    root = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description='RubUP — подготовка заданий TikTok')
    parser.add_argument('--config', default=str(root / 'config.yaml'))
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true', help='Изолированная проверка без сети и ключей')
    group.add_argument('--import-file', metavar='PATH', help='Импорт TXT, CSV или JSON')
    group.add_argument('--prepare', action='store_true', help='Подготовка одного цикла; реальные API могут тарифицироваться')
    group.add_argument('--status', action='store_true', help='Локальная статистика')
    group.add_argument('--export', metavar='PATH', help='Экспорт истории CSV')
    group.add_argument('--gui-smoke', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    gui = not any((args.dry_run, args.import_file, args.prepare, args.status, args.export, args.gui_smoke))
    service = None
    log = None
    try:
        from application import AppService
        from logger import configure_logging
        log = configure_logging(Path(args.config).resolve().parent / 'logs')
        service = AppService(args.config, dry_run_override=True if args.dry_run else None)
        if args.dry_run:
            result = service.dry_run()
        elif args.import_file:
            result = service.import_file(args.import_file)
        elif args.prepare:
            service.refresh()
            result = service.prepare_pending()
        elif args.status:
            result = service.stats()
        elif args.export:
            service.export_csv(args.export)
            result = {'export': str(Path(args.export).resolve())}
        else:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtCore import QTimer
            from gui.main_window import MainWindow
            app = QApplication(sys.argv[:1])
            app.setApplicationName('RubUP Automation')
            window = MainWindow(service)
            window.show()
            if args.gui_smoke:
                QTimer.singleShot(500, window.close)
            return app.exec()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('failed', 0) else 0
    except Exception as exc:
        if isinstance(exc, ImportError):
            detail = 'Не найдены компоненты программы. Выполните установку двойным нажатием install.bat.'
        else:
            detail = str(exc) if isinstance(exc, ValueError) or type(exc).__name__ == 'IntegrationError' else type(exc).__name__
        if log:
            log.error('Операция завершилась ошибкой: %s', detail)
        show_startup_error(detail + '\nПроверьте доступ к папке программы и её установку.', gui=gui)
        return 1
    finally:
        if service:
            service.close()


if __name__ == '__main__':
    raise SystemExit(main())
