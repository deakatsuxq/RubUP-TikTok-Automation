"""Windows desktop entry point and headless maintenance commands."""
import argparse
import json
from pathlib import Path
import sys


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
    from application import AppService
    from logger import configure_logging
    log = configure_logging(Path(args.config).resolve().parent / 'logs')
    service = None
    try:
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
            app.setApplicationName('RubUP TikTok Automation')
            window = MainWindow(service)
            window.show()
            if args.gui_smoke:
                QTimer.singleShot(500, window.close)
            return app.exec()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('failed', 0) else 0
    except Exception as exc:
        from validators import ValidationError
        from api_client import IntegrationError
        detail = str(exc) if isinstance(exc, (ValidationError, IntegrationError, ValueError)) else type(exc).__name__
        log.error('Операция завершилась ошибкой: %s', detail)
        print(f'Ошибка: {detail}', file=sys.stderr)
        return 1
    finally:
        if service:
            service.close()


if __name__ == '__main__':
    raise SystemExit(main())
