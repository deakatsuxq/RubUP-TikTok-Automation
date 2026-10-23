"""Persistent two-account preparation and an honest manual-publication queue.

This module does not publish TikTok comments, click likes or inspect browser
credentials. A stored confirmation is an explicit user attestation only.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
import re
import time

from api_client import IntegrationError
from batch_storage import BatchStore
from budget import BudgetError, BudgetGuard
from comment_publisher import CommentPublisher
from link_resolver import LinkResolver
from profiles import ProfileManager
from scheduler import Scheduler
from task_review import review_task
from telegram_bot import TelegramBot
from validators import ValidationError, canonical_tiktok_url


def _safe_error(error):
    if isinstance(error, (ValidationError, IntegrationError, BudgetError, ValueError)):
        return str(error)[:1000]
    return 'Не удалось завершить действие. Повторите только неуспешную задачу.'


class BatchService:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.store = BatchStore(self.db)
        self.store.ensure_profiles(app.config.account_id)
        self.profiles = ProfileManager(self.store, self.db)

    @contextmanager
    def _guard(self):
        self.app._begin_cycle()
        try:
            yield
        finally:
            self.app._end_cycle()

    def _batch(self, batch_id=None):
        if batch_id is None:
            rows = self.store.list_batches()
            return rows[0] if rows else None
        return self.store.get_batch(batch_id)

    def list_batches(self):
        return self.store.list_batches()

    def _profile_config(self, profile_id):
        config = replace(self.app.config, account_id=profile_id)
        config._base_dir = self.app.root
        return config

    def _publisher(self, profile_id):
        return CommentPublisher(self._profile_config(profile_id), self.db, self.app.images)

    def _known_task(self, task_id, real=False):
        task = self.db.get_task(task_id)
        ids = {row['profile_id'] for row in self.store.list_profiles()}
        if task['account_id'] not in ids:
            raise ValidationError('Задание не принадлежит одному из двух настроенных аккаунтов')
        if real and (self.app.config.dry_run or task.get('is_test')):
            raise ValidationError('Демонстрационные задания не подтверждают реальные действия')
        return task

    def _ensure_tasks(self, batch, video_id):
        return [self.db.ensure_task(profile_id, video_id) for profile_id in batch['profile_ids']]

    def import_links(self, text, batch_id=None):
        if not isinstance(text, str):
            raise ValidationError('Вставьте ссылки по одной на строку')
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or len(lines) > 100:
            raise ValidationError('За один импорт добавьте от 1 до 100 ссылок')
        valid, errors = [], []
        for position, value in enumerate(lines, 1):
            try:
                normalized, _ = LinkResolver._validate_destination(value)
                valid.append(normalized)
            except ValidationError:
                errors.append(f'Строка {position}: нужна безопасная ссылка на видео TikTok')
        with self._guard():
            batch = self._batch(batch_id) if batch_id is not None else None
            if batch is None:
                profiles = self.store.ensure_profiles(self.app.config.account_id)
                batch = self.store.create_batch([row['profile_id'] for row in profiles],
                    limit_usd=getattr(self.app.config, 'batch_budget_usd', None),
                    is_test=self.app.config.dry_run)
            if bool(batch.get('is_test')) != bool(self.app.config.dry_run):
                raise ValidationError('Тестовая очередь отделена от рабочих заданий')
            result = self.store.add_inputs(batch['id'], valid)
            for item in result.get('items', []):
                try:
                    canonical, video_id = canonical_tiktok_url(item['source_url'])
                except ValidationError:
                    continue  # Short URLs remain saved before any network call.
                existing = self.store.find_item(batch['id'], video_id, exclude_id=item['id'])
                if existing:
                    self.store.update_item(item['id'], video_id=video_id, state='DUPLICATE')
                    result['accepted'] -= 1
                    result['duplicates'] += 1
                    continue
                if not self.db.get_video(video_id):
                    self.app.search.add_url(canonical, is_test=self.app.config.dry_run)
                self.store.update_item(item['id'], video_id=video_id, state='RESOLVED')
                self._ensure_tasks(batch, video_id)
            self.store.set_batch_state(batch['id'], 'QUEUED')
            summary = self.summary(batch['id'])
            return {'batch_id': batch['id'], 'accepted': result['accepted'],
                    'duplicates': result['duplicates'], 'invalid': len(errors),
                    'awaiting_review': summary['awaiting_review'], 'errors': errors}

    def import_txt(self, path, batch_id=None):
        source = Path(path)
        if source.stat().st_size > 1024 * 1024:
            raise ValidationError('Текстовый файл должен быть не больше 1 МБ')
        return self.import_links(source.read_text(encoding='utf-8-sig'), batch_id)

    def groups(self, batch_id=None):
        batch = self._batch(batch_id)
        if not batch:
            return []
        profiles = {row['profile_id']: row for row in self.store.list_profiles()}
        tasks = {(row['account_id'], row['video_id']): row for row in self.db.list_tasks()}
        result = []
        for item in self.store.list_items(batch['id']):
            if item['state'] == 'DUPLICATE':
                continue
            video = self.db.get_video(item['video_id']) if item.get('video_id') else None
            pair = []
            if video:
                for profile_id in batch['profile_ids']:
                    row = tasks.get((profile_id, item['video_id']))
                    if row:
                        profile = profiles.get(profile_id, {'profile_id': profile_id, 'slot': len(pair) + 1})
                        pair.append({**row, 'slot': profile['slot'], 'profile': profile,
                                     'review': review_task(row, self.app.config)})
            review = review_task(video, self.app.config) if video else {
                'eligible': False, 'needs_review': True, 'missing': ['date', 'topic'],
                'reason': 'Короткая ссылка ещё не раскрыта'}
            result.append({**(video or {}), 'video_id': item.get('video_id'),
                           'url': video['url'] if video else item['source_url'],
                           'source_url': item['source_url'], 'item_id': item['id'],
                           'batch_id': batch['id'], 'state': item['state'],
                           'error': item.get('error') or '', 'review': review,
                           'tasks': pair, 'is_test': bool(batch.get('is_test'))})
        return result

    def summary(self, batch_id=None):
        batch = self._batch(batch_id)
        groups = self.groups(batch['id']) if batch else []
        tasks = [task for group in groups for task in group['tasks']]
        urls = {group['url'] for group in groups if group.get('video_id')}
        telegram_sent = sum(self.db.telegram_status(url) == 'SENT' for url in urls)
        budget = (BudgetGuard(self.db, batch['id'], self.app.config,
                             limit_usd=batch.get('limit_usd')).summary() if batch else {})
        return {'batch_id': batch['id'] if batch else None,
                'state': batch['state'] if batch else 'QUEUED',
                'videos_total': len(groups),
                'videos_ready': sum(len(group['tasks']) == 2 and all(task.get('comment') for task in group['tasks']) for group in groups),
                'comments_total': len(groups) * 2,
                'comments_ready': sum(bool(task.get('comment')) for task in tasks),
                'published_total': sum(task['publication_status'] == 'PUBLISHED' for task in tasks),
                'telegram_sent': telegram_sent,
                'awaiting_review': sum(not group['review']['eligible'] for group in groups),
                'failed': sum(group['state'] == 'FAILED' for group in groups) + sum(task['processing_status'] == 'FAILED' for task in tasks),
                'profile_ids': batch['profile_ids'] if batch else [], 'budget': budget,
                'total_cost': budget.get('spent_usd') if budget.get('total_cost_known', True) else None,
                'is_test': bool(batch and batch.get('is_test'))}

    def estimate(self, batch_id):
        groups = [group for group in self.groups(batch_id)
                  if not group.get('video_id') or group['review']['eligible']
                  or group['review'].get('preliminary_allowed')]
        count = sum(2 if not group['tasks'] else sum(not task.get('comment') and task['publication_status'] != 'PUBLISHED'
                                                     for task in group['tasks']) for group in groups)
        estimate = self.app.costs.estimate(count=count, input_tokens=250, output_tokens=40)
        return {**estimate, 'requests': count, 'max_requests': count * 3,
                'note': 'Предварительная оценка по 250 входным и 40 выходным токенам. Исправления могут увеличить число запросов до трёх на комментарий.'}

    def _checkpoint(self, batch_id, stop_event, pause_event):
        if pause_event and pause_event.is_set():
            self.store.set_batch_state(batch_id, 'PAUSED')
        okay = Scheduler.checkpoint(stop_event, pause_event)
        if okay:
            self.store.set_batch_state(batch_id, 'PREPARING')
        return okay

    def _pace(self, deadline, stop_event, pause_event):
        if self.app.config.dry_run:
            return True
        while time.monotonic() < deadline:
            if not Scheduler.checkpoint(stop_event, pause_event):
                return False
            duration = min(.1, max(0, deadline - time.monotonic()))
            if stop_event:
                if stop_event.wait(duration):
                    return False
            else:
                time.sleep(duration)
        return True

    def _emit(self, batch_id, progress, message):
        if progress:
            progress({**self.summary(batch_id), 'message': message})

    def _prepare_task(self, task, batch, guard, regenerate=False):
        if task['publication_status'] == 'PUBLISHED':
            return False
        publisher = self._publisher(task['account_id'])
        if task.get('comment') and not regenerate:
            return False
        review = review_task(task, self.app.config)
        if not review['eligible'] and not review['preliminary_allowed']:
            self.db.update_task(task['id'], processing_status='SKIPPED', error=review['reason'])
            return False
        try:
            publisher.prepare_requirements(task['id'])
            context = {**self.db.get_task(task['id']), 'category': review['category'],
                       'account_slot': self.store.get_profile(task['account_id'])['slot']}
            text = self.app.generator.generate(task['id'], context, budget_guard=guard)
            publisher.prepare(task['id'], text, allow_preliminary=True)
            return True
        except BudgetError:
            raise
        except Exception as exc:
            state = self.db.get_task(task['id'])['processing_status']
            self.db.update_task(task['id'], processing_status='SKIPPED' if state == 'SKIPPED' else 'FAILED', error=_safe_error(exc))
            self.db.add_event(task['id'], 'BATCH_GENERATION_FAILED', _safe_error(exc))
            raise

    def prepare(self, batch_id, stop_event=None, pause_event=None, progress=None, retry_failed=False):
        with self._guard():
            batch = self._batch(batch_id)
            if bool(batch.get('is_test')) != bool(self.app.config.dry_run):
                raise ValidationError('Демонстрационная очередь не может использовать рабочие подключения')
            result = {'prepared': 0, 'failed': 0, 'skipped': 0, 'stopped': False, 'budget_blocked': False}
            guard = BudgetGuard(self.db, batch_id, self.app.config, limit_usd=batch.get('limit_usd'))
            next_request = 0
            for original in self.store.list_items(batch_id):
                if original['state'] in {'DUPLICATE', 'SKIPPED'}:
                    continue
                if original['state'] == 'FAILED' and not retry_failed:
                    continue
                if not self._checkpoint(batch_id, stop_event, pause_event):
                    result['stopped'] = True
                    break
                item = self.store.get_item(original['id'])
                self._emit(batch_id, progress, 'Проверяем ссылку…')
                if not self._checkpoint(batch_id, stop_event, pause_event):
                    result['stopped'] = True
                    break
                try:
                    if not item.get('video_id'):
                        canonical, video_id = self.app.search.resolve_url(item['source_url'])
                        duplicate = self.store.find_item(batch_id, video_id, exclude_id=item['id'])
                        if duplicate:
                            self.store.update_item(item['id'], video_id=video_id, state='DUPLICATE')
                            continue
                        if not self.db.get_video(video_id):
                            self.app.search.add_url(canonical, is_test=self.app.config.dry_run)
                        self.store.update_item(item['id'], video_id=video_id, state='RESOLVED', error='')
                        item = self.store.get_item(item['id'])
                    tasks = self._ensure_tasks(batch, item['video_id'])
                    video = self.db.get_video(item['video_id'])
                    if video.get('is_test') and not self.app.config.dry_run:
                        raise ValidationError('Тестовые данные нельзя обработать как реальные задания')
                    protected_metadata = any(row['publication_status'] == 'PUBLISHED'
                        for row in self.db.list_tasks() if row['video_id'] == item['video_id'])
                    if (not video.get('description') and not item.get('metadata_attempted')
                            and not self.app.config.dry_run and not protected_metadata):
                        self._emit(batch_id, progress, 'Получаем доступные сведения…')
                        if not self._checkpoint(batch_id, stop_event, pause_event):
                            result['stopped'] = True
                            break
                        self.store.update_item(item['id'], metadata_attempted=True)
                        from tiktok_search import DiscoveryError
                        try:
                            self.app.search.enrich(item['video_id'])
                        except DiscoveryError:
                            self.db.add_event(tasks[0]['id'], 'BATCH_METADATA_UNAVAILABLE', 'Сведения TikTok недоступны; черновик требует ручной проверки')
                    video = self.db.get_video(item['video_id'])
                    review = review_task(video, self.app.config)
                    if not review['eligible'] and not review['preliminary_allowed']:
                        for task in tasks:
                            if not task.get('comment') and task['publication_status'] != 'PUBLISHED':
                                self.db.update_task(task['id'], processing_status='SKIPPED', error=review['reason'])
                        self.store.update_item(item['id'], state='SKIPPED', error=review['reason'])
                        result['skipped'] += 1
                        continue
                    for initial in tasks:
                        task = self.db.get_task(initial['id'])
                        if task.get('comment') or task['publication_status'] == 'PUBLISHED':
                            continue
                        if task['processing_status'] in {'FAILED', 'SKIPPED'} and not retry_failed:
                            continue
                        if not self._checkpoint(batch_id, stop_event, pause_event):
                            result['stopped'] = True
                            break
                        self._emit(batch_id, progress, 'Готовим комментарий для аккаунта…')
                        if not self._checkpoint(batch_id, stop_event, pause_event) or not self._pace(next_request, stop_event, pause_event):
                            result['stopped'] = True
                            break
                        try:
                            if self._prepare_task(task, batch, guard):
                                result['prepared'] += 1
                            next_request = time.monotonic() + 60 / self.app.config.target_per_minute
                        except BudgetError as exc:
                            result['budget_blocked'] = True
                            result['error'] = _safe_error(exc)
                            self.db.add_event(task['id'], 'BATCH_BUDGET_BLOCKED', _safe_error(exc))
                            break
                        except Exception:
                            result['failed'] += 1
                    current = [self.db.get_task(task['id']) for task in tasks]
                    self.store.update_item(item['id'], state='READY' if all(task.get('comment') for task in current) else 'RESOLVED', error='')
                except BudgetError as exc:
                    result['budget_blocked'] = True
                    result['error'] = _safe_error(exc)
                except Exception as exc:
                    self.store.update_item(item['id'], state='FAILED', error=_safe_error(exc))
                    result['failed'] += 1
                if result['stopped'] or result['budget_blocked']:
                    break
                self._emit(batch_id, progress, 'Проверяем результат…')
            state = 'STOPPED' if result['stopped'] or result['budget_blocked'] else 'READY'
            self.store.set_batch_state(batch_id, state)
            self._queue_eligible(batch_id)
            return {**result, 'summary': self.summary(batch_id)}

    def _batch_for_task(self, task):
        for batch in self.store.list_batches():
            if task['account_id'] in batch['profile_ids'] and any(item.get('video_id') == task['video_id'] and item['state'] != 'DUPLICATE'
                                                                for item in self.store.list_items(batch['id'])):
                return batch
        raise ValidationError('Задание не найдено в пакетной очереди')

    def retry_task(self, task_id):
        return self._single_task(task_id, regenerate=False)

    def regenerate(self, task_id):
        return self._single_task(task_id, regenerate=True)

    def _single_task(self, task_id, regenerate=False):
        with self._guard():
            task = self._known_task(task_id)
            if task.get('is_test') and not self.app.config.dry_run:
                raise ValidationError('Это тестовое задание')
            if task['publication_status'] == 'PUBLISHED':
                raise ValidationError('Публикация для этого аккаунта и видео уже подтверждена')
            if task.get('comment') and not regenerate:
                return {**task, 'review': review_task(task, self.app.config)}
            batch = self._batch_for_task(task)
            guard = BudgetGuard(self.db, batch['id'], self.app.config, limit_usd=batch.get('limit_usd'))
            self._prepare_task(task, batch, guard, regenerate=regenerate)
            self._queue_eligible(batch['id'])
            current = self.db.get_task(task_id)
            return {**current, 'review': review_task(current, self.app.config)}

    def save_comment(self, task_id, text):
        with self._guard():
            task = self._known_task(task_id)
            batch = self._batch_for_task(task)
            result = self._publisher(task['account_id']).prepare(task_id, text, allow_preliminary=True)
            self._queue_eligible(batch['id'])
            return result

    def update_metadata(self, video_id, published_at, description, author=''):
        with self._guard():
            tasks = [task for task in self.db.list_tasks() if task['video_id'] == str(video_id)]
            if any(task['publication_status'] == 'PUBLISHED' for task in tasks):
                raise ValidationError('Сведения видео с подтверждённой публикацией защищены')
            if published_at:
                date.fromisoformat(published_at)
            self.db.update_video(video_id, published_at=published_at or None, description=description,
                                 author=author, hashtags=re.findall(r'#([\w]+)', description, re.UNICODE))
            review = review_task(self.db.get_video(video_id), self.app.config)
            for task in tasks:
                checks = {**task['checklist'], 'preliminary': not review['eligible']}
                state = ('PENDING_APPROVAL' if task.get('comment') and review['eligible'] else
                         'DRAFT_READY' if task.get('comment') and review['preliminary_allowed'] else
                         'ANALYZED' if review['eligible'] else 'NEW')
                self.db.update_task(task['id'], checklist=checks, processing_status=state,
                                    error='' if review['eligible'] else review['reason'])
                self.db.add_event(task['id'], 'MANUAL_METADATA', 'Сведения видео проверены пользователем')
            # Explicitly corrected evidence unblocks this durable queue entry;
            # an old SKIPPED marker must not strand otherwise valid account pairs.
            for batch in self.store.list_batches():
                pair = [task for task in self.db.list_tasks() if task['video_id'] == str(video_id)
                        and task['account_id'] in batch['profile_ids']]
                for item in self.store.list_items(batch['id']):
                    if item.get('video_id') != str(video_id) or item['state'] == 'DUPLICATE':
                        continue
                    if review['eligible'] or review['preliminary_allowed']:
                        state = 'READY' if len(pair) == 2 and all(task.get('comment') for task in pair) else 'RESOLVED'
                        self.store.update_item(item['id'], state=state, error='', metadata_attempted=True)
                    else:
                        self.store.update_item(item['id'], state='SKIPPED', error=review['reason'], metadata_attempted=True)
                self._queue_eligible(batch['id'])
            return {**self.db.get_video(video_id), 'review': review}

    def set_requirements(self, task_id, require_image, required_image_slot=None, require_like=False):
        with self._guard():
            task = self._known_task(task_id)
            self._batch_for_task(task)
            if task['publication_status'] == 'PUBLISHED':
                raise ValidationError('Требования подтверждённой публикации защищены')
            if type(require_image) is not bool or type(require_like) is not bool:
                raise ValidationError('Укажите требования картинки и лайка')
            if required_image_slot is not None and (type(required_image_slot) is not int or required_image_slot not in (1, 2, 3, 4) or not require_image):
                raise ValidationError('Номер обязательной картинки должен быть от 1 до 4')
            checks = dict(task['checklist'])
            for key in ('image_sha256', 'published', 'comment_present', 'comment_not_deleted', 'image_attached', 'own_comment_liked'):
                checks.pop(key, None)
            checks.update(require_image=require_image, required_image_slot=required_image_slot, require_like=require_like)
            self.db.update_task(task_id, checklist=checks, image_slot=None, expected_reward=0)
            if task.get('comment'):
                return self._publisher(task['account_id']).prepare(task_id, task['comment'], allow_preliminary=True)
            return self.db.get_task(task_id)

    def confirm_publication(self, task_id, checks):
        with self._guard():
            task = self._known_task(task_id, real=True)
            batch = self._batch_for_task(task)
            self._publisher(task['account_id']).validate_draft(task, task['comment'])
            if task.get('image_slot'):
                image = self.app.images.get_image(task['image_slot'])
                if not image or not self.app.images.verify(task['image_slot']) or image['sha256'] != task['checklist'].get('image_sha256'):
                    raise ValidationError('Оригинал картинки изменён после подготовки')
            result = self.app.report_tracker.confirm_publication(task_id, checks, allow_pending_like=True)
            self._queue_eligible(batch['id'])
            return result

    def confirm_like(self, task_id):
        with self._guard():
            task = self._known_task(task_id, real=True)
            batch = self._batch_for_task(task)
            if task['publication_status'] != 'PUBLISHED':
                raise ValidationError('Сначала подтвердите фактическую публикацию комментария')
            self.db.update_task(task_id, checklist={**task['checklist'], 'own_comment_liked': True})
            self.db.add_event(task_id, 'MANUAL_LIKE_CONFIRMED', 'Лайк собственному комментарию подтверждён пользователем')
            self._queue_eligible(batch['id'])
            return self.db.get_task(task_id)

    @staticmethod
    def _complete(task):
        checks = task.get('checklist') or {}
        return (task['publication_status'] == 'PUBLISHED' and checks.get('comment_present') is True
                and checks.get('comment_not_deleted') is True
                and (not checks.get('require_image') or checks.get('image_attached') is True)
                and (not checks.get('require_like') or checks.get('own_comment_liked') is True))

    def _group_eligible(self, group):
        if group.get('is_test') or self.app.config.dry_run or not group['review']['eligible']:
            return False
        tasks = group['tasks']
        if len(tasks) != 2:
            return False
        mode = getattr(self.app.config, 'batch_telegram_mode', 'both')
        if mode == 'both':
            return all(self._complete(task) for task in tasks)
        if mode == 'any':
            return any(self._complete(task) for task in tasks)
        if mode == 'draft':
            return all(task.get('comment') and task['processing_status'] in {'DRAFT_READY', 'PENDING_APPROVAL', 'PUBLISHED'} for task in tasks)
        return mode == 'verified'

    def _queue_eligible(self, batch_id):
        added = 0
        for group in self.groups(batch_id):
            if self._group_eligible(group):
                if self.db.telegram_status(group['url']) == 'SENT':
                    continue
                added += bool(self.store.enqueue_telegram(group['url'], group['video_id'], batch_id))
        return added

    def queue_eligible(self, batch_id):
        with self._guard():
            return self._queue_eligible(batch_id)

    def drain_telegram(self, batch_id=None):
        with self._guard():
            result = {'sent': 0, 'failed': 0, 'skipped': 0, 'unknown': 0}
            if self.app.config.dry_run:
                return {**result, 'is_test': True}
            rows = self.store.telegram_queue(batch_id)
            for pending in rows:
                if pending['status'] in {'SENT', 'UNKNOWN'}:
                    continue
                if pending['status'] == 'IN_FLIGHT' and self.db.telegram_status(pending['url']) == 'SENT':
                    self.store.update_telegram(pending['url'], 'SENT')
                    result['skipped'] += 1
                    continue
                if pending['status'] == 'IN_FLIGHT':
                    self.store.update_telegram(pending['url'], 'UNKNOWN', 'Предыдущая отправка прервалась; проверьте чат вручную')
                    result['unknown'] += 1
                    continue
                group = next((row for row in self.groups(pending['batch_id']) if row.get('video_id') == pending['video_id']), None)
                if not group or not self._group_eligible(group):
                    result['skipped'] += 1
                    continue
                existing = self.db.telegram_status(pending['url'])
                if existing in {'SENT', 'UNKNOWN', 'RESERVED', 'IN_FLIGHT', 'SENDING'}:
                    status = 'SENT' if existing == 'SENT' else 'UNKNOWN'
                    self.store.update_telegram(pending['url'], status, '' if status == 'SENT' else 'Результат предыдущей доставки требует ручной проверки')
                    result['skipped'] += 1
                    continue
                mode = getattr(self.app.config, 'batch_telegram_mode', 'both')
                config = replace(self.app.config, telegram_mode='published' if mode in {'both', 'any'} else mode)
                bot = TelegramBot(config, self.db,
                    token=self.app.credential_store.get('TELEGRAM_BOT_TOKEN'),
                    chat_id=self.app.credential_store.get('TELEGRAM_CHAT_ID'))
                self.store.update_telegram(pending['url'], 'IN_FLIGHT')
                candidate = next((task for task in group['tasks'] if task['publication_status'] == 'PUBLISHED'), group['tasks'][0])
                if mode == 'verified':
                    candidate = {**candidate, 'processing_status': 'ANALYZED'}
                try:
                    sent = bot.send_video(candidate)
                    status = self.db.telegram_status(pending['url'])
                    if sent or status == 'SENT':
                        self.store.update_telegram(pending['url'], 'SENT')
                        result['sent'] += 1
                    else:
                        status = 'UNKNOWN' if status in {'UNKNOWN', 'RESERVED', 'IN_FLIGHT', 'SENDING'} else 'FAILED'
                        self.store.update_telegram(pending['url'], status, 'Отправка не подтверждена')
                        result['unknown' if status == 'UNKNOWN' else 'failed'] += 1
                except Exception as exc:
                    uncertain = self.db.telegram_status(pending['url']) in {'UNKNOWN', 'RESERVED', 'IN_FLIGHT', 'SENDING'}
                    status = 'UNKNOWN' if uncertain else 'FAILED'
                    self.store.update_telegram(pending['url'], status, _safe_error(exc))
                    result['unknown' if uncertain else 'failed'] += 1
                finally:
                    bot.close()
            return result

    def open_video(self, task_id):
        task = self._known_task(task_id, real=True)
        return self.profiles.open_video(task['account_id'], task['url'])

    def open_profile(self, profile_id):
        return self.profiles.open_profile(profile_id)

    def demo(self):
        """A separate 50-video/100-comment local demonstration, never a delivery."""
        from application import AppService
        from image_manager import ImageManager
        from models import Video
        import tempfile
        import os
        # Allocate exclusively rather than reusing any configured or existing
        # filename: a user's real database can never be mistaken for a demo.
        directory = self.app.root / 'data/batch_demo_sessions'
        directory.mkdir(parents=True, exist_ok=True)
        fd, session_path = tempfile.mkstemp(prefix='session-', suffix='.sqlite3', dir=directory)
        os.close(fd)
        cfg = replace(self.app.config, dry_run=False,
            db_path=session_path, account_id='demo-account-1',
            require_image=False, require_like=False, image_mode='none', batch_telegram_mode='disabled',
            hashtags=['cs2', 'telegram'], disabled_hashtags=[], keywords=[], min_date='2026-05-01',
            max_comment_length=max(100, self.app.config.max_comment_length), max_videos=100,
            advertising=True, disclosure='реклама')
        demo = AppService(self.app.config_path, config=cfg)
        cfg.dry_run = True  # Isolate the actual DB path before enabling no-network clients.
        demo.images = ImageManager(directory / (Path(session_path).stem + '-images'), demo.db)
        demo.publisher.images = demo.images
        try:
            batch = BatchService(demo)
            urls = []
            for index in range(50):
                video_id = str(9900000000000000000 + index)
                url = f'https://www.tiktok.com/@rubup_test_fixture/video/{video_id}'
                demo.db.add_video(Video(video_id, url, author='ТЕСТОВЫЕ ДАННЫЕ',
                    description='#cs2 тестовый контекст скинов', hashtags=['cs2'],
                    published_at='2026-06-01', category='CS2', is_test=True))
                urls.append(url)
            previous = next((row for row in batch.list_batches() if row.get('is_test')), None)
            imported = batch.import_links('\n'.join(urls), previous['id'] if previous else None)
            batch.prepare(imported['batch_id'])
            return {'batch_id': imported['batch_id'], 'groups': batch.groups(imported['batch_id']),
                    'summary': batch.summary(imported['batch_id']), 'database': session_path, 'is_test': True, 'demo': True}
        finally:
            demo.close()

    def close(self):
        # Storage connections are operation-scoped; the parent owns shared clients.
        return None
