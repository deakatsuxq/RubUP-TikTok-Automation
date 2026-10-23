"""Shared application operations for CLI and Qt. Importing does not perform IO."""
from dataclasses import replace
from functools import wraps
from pathlib import Path
import re
import threading
import time

from ai_comment_generator import CommentGenerator
from comment_publisher import CommentPublisher
from config import load_config, save_config
from cost_tracker import CostTracker
from database import Database
from image_manager import ImageManager
from models import Video
from report_tracker import ReportTracker
from scheduler import ProcessLock, Scheduler
from telegram_bot import TelegramBot
from tiktok_search import SearchService
from validators import ValidationError
from video_analyzer import analyze_video
from task_review import review_task
from credentials import CredentialStore
from user_state import UserState
from settings_service import SettingsService


def _serialized(operation):
    """Do not let settings close clients or change evidence during an operation."""
    @wraps(operation)
    def guarded(self, *args, **kwargs):
        self._begin_cycle()
        try:
            return operation(self, *args, **kwargs)
        finally:
            self._end_cycle()
    return guarded


class AppService:
    def __init__(self, config_path='config.yaml', dry_run_override=None, *, config=None):
        self.config_path = Path(config_path).resolve()
        self.root = self.config_path.parent
        if config is None and not self.config_path.exists():
            from config import Config
            config = Config(db_path=str(self.root / 'data/history.sqlite3'))
            config._base_dir = self.root
            save_config(config, self.config_path)
        self.config = config or load_config(self.config_path)
        if dry_run_override is not None:
            self.config = replace(self.config, dry_run=dry_run_override)
        db_path = self.config.db_path
        if self.config.dry_run:
            db_path = str(self.root / 'data' / 'dry_run.sqlite3')
        elif Path(db_path).resolve() == self.root / 'data' / 'dry_run.sqlite3':
            raise ValueError('data/dry_run.sqlite3 зарезервирована для тестового режима')
        self.db = Database(db_path)
        self.images = ImageManager(
            self.root / ('data/dry_run_images' if self.config.dry_run else 'images'), self.db,
        )
        self.credential_store = CredentialStore(self.root)
        self.user_state = UserState(self.root / 'data/ui_state.json')
        self.search = SearchService(self.config, self.db)
        self.generator = CommentGenerator(self.config, self.db, api_key=self.credential_store.get('OPENAI_API_KEY'))
        self.telegram = TelegramBot(self.config, self.db,
                                    token=self.credential_store.get('TELEGRAM_BOT_TOKEN'),
                                    chat_id=self.credential_store.get('TELEGRAM_CHAT_ID'))
        self.publisher = CommentPublisher(self.config, self.db, self.images)
        self.report_tracker = ReportTracker(self.db)
        self.cost_tracker = CostTracker(self.db, self.config)
        self.costs = self.cost_tracker
        self._cycle_lock = threading.Lock()
        self._process_lock = ProcessLock(str(db_path) + '.worker.lock')
        self.settings = SettingsService(self)
        self._batch = None

    @property
    def batch(self):
        if self._batch is None:
            from batch_service import BatchService
            self._batch = BatchService(self)
        return self._batch

    def _begin_cycle(self):
        if not self._cycle_lock.acquire(blocking=False):
            raise ValidationError('Другой цикл обработки уже выполняется')
        try:
            acquired = self._process_lock.acquire()
        except Exception:
            self._cycle_lock.release()
            raise
        if not acquired:
            self._cycle_lock.release()
            raise ValidationError('Другой экземпляр приложения уже обрабатывает эту базу')

    def _end_cycle(self):
        self._process_lock.release()
        self._cycle_lock.release()

    def list_tasks(self):
        return self.db.list_tasks(self.config.account_id)

    def stats(self):
        return self.db.stats()

    def task_view(self, task_or_id):
        task = dict(task_or_id) if isinstance(task_or_id, dict) else self.db.get_task(task_or_id)
        return {**task, 'review': review_task(task, self.config)}

    def onboarding_complete(self):
        return bool(self.user_state.get('onboarding_complete', False))

    def complete_onboarding(self):
        self.user_state.set('onboarding_complete', True)

    def reset_onboarding(self):
        self.user_state.set('onboarding_complete', False)

    def process_url(self, url, stop_event=None, progress=None):
        """Prepare one known link, preserving cached drafts and pending evidence."""
        self._begin_cycle()
        task = None
        warning = ''

        def checkpoint(message):
            if stop_event and stop_event.is_set():
                return False
            if progress:
                progress(message)
            return not (stop_event and stop_event.is_set())

        def cancelled():
            if task:
                return {**self.task_view(task['id']), 'cancelled': True, 'warning': warning}
            return {'cancelled': True}

        try:
            if not checkpoint('Проверяем ссылку…'):
                return cancelled()
            canonical, video_id = self.search.resolve_url(url)
            existing = self.db.get_video(video_id)
            if not existing:
                self.search.add_url(canonical)
            task = self.db.ensure_task(self.config.account_id, video_id)
            if task['publication_status'] == 'PUBLISHED':
                return {**self.task_view(task), 'cached': True}
            if task.get('is_test') and not self.config.dry_run:
                raise ValidationError('Тестовые данные доступны только в демонстрационном режиме')
            # Cached preparation is a result, never a reason for another paid call.
            if task.get('comment'):
                return {**self.task_view(task), 'cached': True}
            if not checkpoint('Получаем доступные сведения…'):
                return cancelled()
            if not task.get('description') and not self.config.dry_run:
                from tiktok_search import DiscoveryError
                try:
                    self.search.enrich(video_id)
                except DiscoveryError:
                    warning = 'Не удалось получить сведения из TikTok. Недостающие данные можно проверить вручную.'
                    self.db.add_event(task['id'], 'METADATA_UNAVAILABLE', warning)
            task = self.db.get_task(task['id'])
            review = review_task(task, self.config)
            if not review['eligible'] and not review['preliminary_allowed']:
                self.db.update_task(task['id'], processing_status='SKIPPED', error=review['reason'])
                raise ValidationError(review['reason'])
            if not checkpoint('Готовим комментарий…'):
                return cancelled()
            result = self._prepare_one(task['id'], allow_preliminary=True)
            if not checkpoint('Проверяем результат…'):
                return cancelled()
            return {**self.task_view(result), 'warning': warning, 'cached': False}
        finally:
            self._end_cycle()

    @_serialized
    def import_urls(self, text):
        result = self.search.import_urls(text)
        self.refresh()
        return result

    @_serialized
    def import_file(self, path):
        result = self.search.import_file(path)
        self.refresh()
        return result

    def refresh(self):
        found = self.search.search()
        count = 0
        for video in found[:self.config.max_videos]:
            task = self.db.ensure_task(self.config.account_id, video['video_id'])
            if task['processing_status'] not in ('NEW', 'ANALYZED'):
                continue
            analysis = analyze_video(self.db.get_video(video['video_id']), self.config)
            checklist = dict(task.get('checklist') or {})
            checklist.setdefault('require_image', self.config.require_image)
            checklist.setdefault('require_like', self.config.require_like)
            self.db.update_task(
                task['id'], processing_status='ANALYZED' if analysis['eligible'] else 'NEW',
                error=analysis.get('reason', '') if not analysis['eligible'] else '',
                checklist=checklist,
            )
            count += 1
        return {'found': len(found), 'queued': count, 'source': 'local_import'}

    def prepare_one(self, task_id, regenerate=False, allow_preliminary=False):
        # Also protects GUI/editor versus a concurrent scheduler cycle.
        self._begin_cycle()
        try:
            return self._prepare_one(task_id, regenerate, allow_preliminary=allow_preliminary)
        finally:
            self._end_cycle()

    def _prepare_one(self, task_id, regenerate=False, allow_preliminary=False):
        task = self.db.get_task(task_id)
        if not task:
            raise ValidationError('Задача не найдена')
        if task['account_id'] != self.config.account_id:
            raise ValidationError('Задача принадлежит другому аккаунту')
        if task['publication_status'] == 'PUBLISHED':
            raise ValidationError('Публикация уже подтверждена')
        if task.get('is_test') and not self.config.dry_run:
            raise ValidationError('Тестовые задания не используют платную генерацию')
        analysis = review_task(task, self.config)
        if not analysis['eligible'] and not (allow_preliminary and analysis['preliminary_allowed']):
            raise ValidationError(analysis['reason'])
        if task.get('comment') and not regenerate:
            return self.publisher.prepare(task_id, task['comment'], allow_preliminary=allow_preliminary)
        self.db.update_task(task_id, attempts=task.get('attempts', 0) + 1)
        try:
            self.publisher.prepare_requirements(task_id)
            context = {**self.db.get_task(task_id), 'category': analysis['category']}
            comment = self.generator.generate(task_id, context)
            return self.publisher.prepare(task_id, comment, allow_preliminary=allow_preliminary)
        except Exception as exc:
            # Integrations expose sanitized messages; unknown exceptions only expose type.
            from api_client import IntegrationError
            detail = str(exc) if isinstance(exc, (ValidationError, IntegrationError)) else type(exc).__name__
            status = self.db.get_task(task_id)['processing_status']
            self.db.update_task(task_id, processing_status='SKIPPED' if status == 'SKIPPED' else 'FAILED', error=detail)
            self.db.add_event(task_id, 'FAILED', detail)
            raise

    def prepare_pending(self, stop_event=None, pause_event=None, progress=None):
        self._begin_cycle()
        summary = {'prepared': 0, 'failed': 0, 'manual_review': 0, 'stopped': False}
        try:
            tasks = [t for t in self.list_tasks() if t['processing_status'] in ('NEW', 'ANALYZED')]
            eligible = []
            for task in tasks:
                analysis = analyze_video(self.db.get_video(task['video_id']), self.config)
                if analysis['eligible']:
                    eligible.append(task)
                else:
                    summary['manual_review'] += 1
            eligible = eligible[:self.config.max_videos]
            for index, task in enumerate(eligible):
                if not Scheduler.checkpoint(stop_event, pause_event):
                    summary['stopped'] = True
                    break
                try:
                    self._prepare_one(task['id'])
                    summary['prepared'] += 1
                except Exception:
                    summary['failed'] += 1
                if progress:
                    progress(dict(summary))
                # Preparation API pacing, not a TikTok publication throughput claim.
                if index + 1 < len(eligible):
                    delay = 60 / self.config.target_per_minute
                    if stop_event:
                        if stop_event.wait(delay):
                            summary['stopped'] = True
                            break
                    elif not self.config.dry_run:
                        time.sleep(delay)
            return summary
        finally:
            self._end_cycle()

    def save_comment(self, task_id, text, allow_preliminary=False):
        self._begin_cycle()
        try:
            return self.publisher.prepare(task_id, text, allow_preliminary=allow_preliminary)
        finally:
            self._end_cycle()

    @_serialized
    def set_task_requirements(self, task_id, require_image, required_image_slot=None, require_like=False):
        task = self.db.get_task(task_id)
        if task['account_id'] != self.config.account_id:
            raise ValidationError('Задача принадлежит другому аккаунту')
        if task['publication_status'] == 'PUBLISHED':
            raise ValidationError('Требования подтверждённой публикации защищены')
        if type(require_image) is not bool or type(require_like) is not bool:
            raise ValidationError('Требования задаются логическими значениями')
        if required_image_slot is not None and (type(required_image_slot) is not int or required_image_slot not in (1, 2, 3, 4)):
            raise ValidationError('Номер картинки должен быть от 1 до 4')
        if required_image_slot is not None and not require_image:
            raise ValidationError('Указан номер картинки, но требование картинки выключено')
        checks = dict(task['checklist'])
        before = (checks.get('require_image'), checks.get('required_image_slot'), checks.get('require_like'))
        after = (require_image, required_image_slot, require_like)
        if before == after:
            return task
        for key in ('image_sha256', 'published', 'comment_present', 'image_attached',
                    'own_comment_liked', 'comment_not_deleted'):
            checks.pop(key, None)
        checks.update(require_image=require_image, required_image_slot=required_image_slot, require_like=require_like)
        self.db.update_task(task_id, checklist=checks, image_slot=None,
                            processing_status='ANALYZED', expected_reward=0,
                            error='Требования изменены: повторно проверьте сохранённый черновик')
        if task['comment']:
            return self.publisher.prepare(task_id, task['comment'], allow_preliminary=True)
        return self.db.get_task(task_id)

    @_serialized
    def enrich_metadata(self, task_id):
        task = self.db.get_task(task_id)
        if task['account_id'] != self.config.account_id or task['publication_status'] == 'PUBLISHED':
            raise ValidationError('Метаданные этой задачи защищены')
        if any(t['video_id'] == task['video_id'] and t['publication_status'] == 'PUBLISHED' for t in self.db.list_tasks()):
            raise ValidationError('Метаданные видео с подтверждённой публикацией защищены')
        self.search.enrich(task['video_id'])
        self.refresh()
        return self.db.get_task(task_id)

    @_serialized
    def confirm_publication(self, task_id, checklist):
        if self.config.dry_run:
            raise ValidationError('Dry-run не подтверждает реальные публикации')
        task = self.db.get_task(task_id)
        if task.get('is_test'):
            raise ValidationError('Тестовые данные не подтверждают реальную публикацию')
        self.publisher.validate_draft(task, task['comment'])
        if task.get('image_slot') and not self.images.verify(task['image_slot']):
            raise ValidationError('Оригинал картинки повреждён или недоступен')
        if task.get('image_slot'):
            selected = self.images.get_image(task['image_slot'])
            if task['checklist'].get('image_sha256') != selected['sha256']:
                raise ValidationError('Картинка заменена после подготовки: восстановите оригинал')
        return self.report_tracker.confirm_publication(task_id, checklist)

    @_serialized
    def send_links(self):
        result = {'sent': 0, 'skipped': 0, 'failed': 0}
        if self.config.dry_run:
            return {**result, 'dry_run': True}
        for task in self.list_tasks():
            if not analyze_video(task, self.config)['eligible']:
                result['skipped'] += 1
                continue
            try:
                if self.telegram.send_video(task):
                    result['sent'] += 1
                else:
                    result['skipped'] += 1
            except Exception:
                result['failed'] += 1
        return result

    @_serialized
    def send_task_link(self, task_id):
        task = self.db.get_task(task_id)
        if task['account_id'] != self.config.account_id or task.get('is_test') or self.config.dry_run:
            raise ValidationError('Демонстрационные данные нельзя отправлять в Telegram')
        if not review_task(task, self.config)['eligible']:
            raise ValidationError('Перед отправкой проверьте дату и требования видео')
        if self.config.telegram_mode == 'disabled':
            raise ValidationError('Отправка ссылок выключена. Выберите режим в настройках Telegram.')
        return self.telegram.send_video(task)

    @_serialized
    def send_test_link(self, url):
        """Explicit user action, one real locally verified URL and the same outbox."""
        from validators import canonical_tiktok_url
        if self.config.dry_run:
            raise ValidationError('В демонстрационном режиме сообщения не отправляются')
        canonical, video_id = canonical_tiktok_url(url)
        task = next((t for t in self.list_tasks() if t['video_id'] == video_id), None)
        if not task or task.get('is_test') or not review_task(task, self.config)['eligible']:
            raise ValidationError('Выберите настоящее видео из своих заданий с проверенной датой и тематикой')
        if task['url'] != canonical:
            raise ValidationError('Адрес должен совпадать с проверенным видео из ваших заданий')
        config = replace(self.config, telegram_mode='verified')
        test_bot = TelegramBot(config, self.db,
                               token=self.credential_store.get('TELEGRAM_BOT_TOKEN'),
                               chat_id=self.credential_store.get('TELEGRAM_CHAT_ID'))
        try:
            # Verified metadata is sufficient for this explicit test; no publication is inferred.
            candidate = {**task, 'processing_status': 'ANALYZED'}
            return test_bot.send_video(candidate)
        finally:
            test_bot.close()

    @_serialized
    def update_metadata(self, task_id, published_at, description, author=''):
        task = self.db.get_task(task_id)
        if task['account_id'] != self.config.account_id:
            raise ValidationError('Задача принадлежит другому аккаунту')
        if task['publication_status'] == 'PUBLISHED':
            raise ValidationError('Метаданные подтверждённой публикации защищены')
        if any(t['video_id'] == task['video_id'] and t['publication_status'] == 'PUBLISHED' for t in self.db.list_tasks()):
            raise ValidationError('Метаданные видео с подтверждённой публикацией защищены')
        from datetime import date
        if published_at:
            date.fromisoformat(published_at)
        tags = re.findall(r'#([\w]+)', description, re.UNICODE)
        self.db.update_video(task['video_id'], published_at=published_at or None,
                             description=description, author=author, hashtags=tags)
        self.db.add_event(task_id, 'MANUAL_METADATA', 'Метаданные введены пользователем')
        # Explicitly revalidate this task even when it previously failed.
        analysis = review_task(self.db.get_video(task['video_id']), self.config)
        checks = {**task['checklist'], 'preliminary': not analysis['eligible']}
        if task.get('comment') and analysis['eligible']:
            status = 'PENDING_APPROVAL'
            detail = 'Метаданные изменены: проверьте или перегенерируйте сохранённый комментарий'
        else:
            status = ('DRAFT_READY' if task.get('comment') and analysis['preliminary_allowed']
                      else 'ANALYZED' if analysis['eligible'] else 'NEW')
            detail = '' if analysis['eligible'] else analysis['reason']
        self.db.update_task(task_id, processing_status=status, error=detail, checklist=checks)
        return self.db.get_task(task_id)

    def export_csv(self, path):
        return self.report_tracker.export_csv(path)

    def save_settings(self, config):
        return self.settings.apply(config=config)

    def test_openai(self):
        return self.settings.test_openai()

    def test_telegram(self):
        return self.settings.test_telegram()

    def dry_run(self):
        cfg = replace(self.config, dry_run=True, require_image=False, require_like=False, image_mode='none',
                      hashtags=['cs2', 'telegram'], disabled_hashtags=[], keywords=[], min_date='2026-05-01',
                      max_comment_length=max(100, self.config.max_comment_length), max_videos=2,
                      advertising=True, disclosure='реклама')
        demo = AppService(self.config_path, config=cfg)
        try:
            fixtures = [
                Video(video_id='9990000000000000001',
                      url='https://www.tiktok.com/@rubup_test_fixture/video/9990000000000000001',
                      author='ТЕСТОВЫЕ ДАННЫЕ', description='#cs2 тестовый контекст скинов',
                      hashtags=['cs2'], published_at='2026-06-01', category='CS2', is_test=True),
                Video(video_id='9990000000000000002',
                      url='https://www.tiktok.com/@rubup_test_fixture/video/9990000000000000002',
                      author='ТЕСТОВЫЕ ДАННЫЕ', description='#telegram тестовый контекст подарков',
                      hashtags=['telegram'], published_at='2026-06-02', category='Telegram-подарки', is_test=True),
            ]
            for video in fixtures:
                demo.db.add_video(video)
                demo.db.ensure_task(cfg.account_id, video.video_id)
            demo.refresh()
            result = demo.prepare_pending()
            return {
                'test_data': True, 'database': str(self.root / 'data/dry_run.sqlite3'),
                'network_requests': 0, **result, 'stats': demo.stats(),
                'actions': ['Анализ тестовых метаданных', 'Подготовка тестовых черновиков',
                            'Публикация и отправка Telegram не выполнялись'],
            }
        finally:
            demo.close()

    def demo_result(self):
        report = self.dry_run()
        demo_db = Database(report['database'])
        try:
            candidates = [t for t in demo_db.list_tasks(self.config.account_id) if t.get('comment') and t.get('is_test')]
            if not candidates:
                raise ValidationError('Не удалось подготовить демонстрационный пример')
            return {**self.task_view(candidates[0]), 'demo': True, 'demo_stats': report['stats'],
                    'warning': 'Демонстрационный режим — реальные действия не выполняются'}
        finally:
            demo_db.close()

    def close(self):
        for obj in (self.search, self.generator, self.telegram):
            close = getattr(obj, 'close', None)
            if close:
                close()
        self.db.close()

