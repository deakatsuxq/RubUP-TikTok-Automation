"""Manual preparation only. No TikTok write endpoint is called by this program."""
from validators import ValidationError, canonical_tiktok_url, validate_comment
from video_analyzer import analyze_video
from task_review import review_task


class CommentPublisher:
    def __init__(self, config, db, images):
        self.config, self.db, self.images = config, db, images

    def validate_draft(self, task, comment, allow_preliminary=False):
        if task['account_id'] != self.config.account_id:
            raise ValidationError('Задача принадлежит другому аккаунту')
        _, video_id = canonical_tiktok_url(task['url'])
        if video_id != task['video_id']:
            raise ValidationError('ID видео не соответствует ссылке')
        if task['publication_status'] == 'PUBLISHED':
            raise ValidationError('Публикация уже подтверждена для этого аккаунта и видео')
        analysis = review_task(task, self.config)
        if not analysis['eligible'] and not (allow_preliminary and analysis['preliminary_allowed']):
            raise ValidationError(analysis['reason'])
        return validate_comment(
            comment, self.config.max_comment_length,
            self.db.recent_comments(10000, exclude_task_id=task['id']),
            advertising=self.config.advertising, disclosure=self.config.disclosure,
        )

    def prepare(self, task_id, comment, allow_preliminary=False):
        task = self.db.get_task(task_id)
        if not task:
            raise ValidationError('Задача не найдена')
        text = self.validate_draft(task, comment, allow_preliminary=allow_preliminary)
        if self.db.comment_exists(text, exclude_task_id=task_id):
            raise ValidationError('Этот комментарий уже использован')
        task = self.prepare_requirements(task_id)
        preliminary = not review_task(task, self.config)['eligible']
        checks = {**task['checklist'], 'preliminary': preliminary}
        self.db.update_task(
            task_id, comment=text, processing_status='DRAFT_READY' if preliminary else 'PENDING_APPROVAL',
            error='', checklist=checks,
        )
        if task.get('image_slot'):
            self.images.record_usage(task['image_slot'], task_id)
        detail = ('Предварительный комментарий: дата или тематика ещё не проверены'
                  if preliminary else 'Подготовлен комментарий; требуется ручная публикация')
        self.db.add_event(task_id, 'DRAFT_READY', detail)
        return self.db.get_task(task_id)

    def prepare_requirements(self, task_id):
        """Check local prerequisites before a potentially billable generation."""
        task = self.db.get_task(task_id)
        checklist = dict(task.get('checklist') or {})
        checklist.setdefault('require_image', self.config.require_image)
        checklist.setdefault('require_like', self.config.require_like)
        required_slot = checklist.get('required_image_slot')
        if required_slot is not None:
            checklist['require_image'] = True
        image = None
        # No image is attached unless a task explicitly requires it.
        if checklist['require_image']:
            if task.get('image_slot'):
                image = self.images.get_image(task['image_slot'])
                if not image or not self.images.verify(image['slot']):
                    raise ValidationError('Оригинал выбранной картинки отсутствует или повреждён')
                if checklist.get('image_sha256') and image['sha256'] != checklist['image_sha256']:
                    raise ValidationError('Картинка заменена после выбора: восстановите исходный оригинал')
            else:
                image = self.images.select(
                    mode='task' if required_slot is not None else self.config.image_mode,
                    order=self.config.image_order,
                    specific=self.config.selected_image,
                    required_slot=required_slot,
                )
            if not image:
                raise ValidationError('Задание требует картинку: добавьте оригинал и выберите режим')
            if required_slot is not None and image['slot'] != required_slot:
                raise ValidationError('Картинка не соответствует номеру в задании')
            checklist['image_sha256'] = image['sha256']
        slot = image['slot'] if image else None
        rewards = self.config.reward_by_image
        expected = float(rewards.get(str(slot), rewards.get(slot, self.config.reward_default)))
        self.db.update_task(
            task_id, image_slot=slot, checklist=checklist, expected_reward=expected,
        )
        return self.db.get_task(task_id)

