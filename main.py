"""RubUP 4.0 entry point: a user comment bank and manual Playwright workflow."""
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
    parser = argparse.ArgumentParser(description='RubUP 4.0 — ваши комментарии и Playwright')
    parser.add_argument('--config', default=str(root / 'config.yaml'))
    parser.add_argument('--test-mode', action='store_true', help='Изолированная тестовая очередь без внешних действий')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true', help='Проверка 50 видео / 100 комментариев без сети')
    group.add_argument('--batch-demo', action='store_true', help='Демонстрация 50 видео / 100 комментариев без сети')
    group.add_argument('--import-file', metavar='PATH', help='Импорт TikTok-ссылок из TXT')
    group.add_argument('--import-comments', metavar='PATH', help='Импорт ваших комментариев из TXT')
    group.add_argument('--prepare', action='store_true', help='Назначить комментарии из банка; без публикаций')
    group.add_argument('--status', action='store_true', help='Локальная статистика')
    group.add_argument('--export', metavar='PATH', help='Экспорт истории CSV')
    group.add_argument('--gui-smoke', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    gui = not any((args.dry_run, args.batch_demo, args.import_file, args.import_comments,
                   args.prepare, args.status, args.export, args.gui_smoke))
    service = None
    log = None
    try:
        from automation_service import AutomationService
        from logger import configure_logging
        # Isolated demonstrations do not write logs or queues into the user's
        # working directory. The service chooses a fresh temporary root.
        service = AutomationService(args.config, test_mode=args.test_mode or args.dry_run or args.batch_demo)
        log = configure_logging(service.root / 'logs')
        if args.batch_demo or args.dry_run:
            result = service.demo()
        elif args.import_file:
            result = service.import_links(Path(args.import_file).read_text(encoding='utf-8-sig'))
        elif args.import_comments:
            result = service.bank.import_text(Path(args.import_comments).read_text(encoding='utf-8-sig'))
        elif args.prepare:
            result = service.prepare()
        elif args.status:
            result = service.stats()
        elif args.export:
            import csv
            rows = service.tasks()
            with Path(args.export).open('w', encoding='utf-8-sig', newline='') as output:
                writer = csv.DictWriter(output, fieldnames=['id', 'slot', 'video_id', 'url', 'comment', 'status', 'published', 'liked'], extrasaction='ignore')
                writer.writeheader()
                writer.writerows(rows)
            result = {'export': str(Path(args.export).resolve())}
        else:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtCore import QTimer
            from gui.automation_window import AutomationWindow
            app = QApplication(sys.argv[:1])
            app.setApplicationName('RubUP Automation 4.0')
            window = AutomationWindow(service)
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
