"""Explain missing evidence without treating a preliminary comment as approval."""
from datetime import date

from video_analyzer import analyze_video, extract_hashtags, parse_publication_date


def review_task(task, config):
    analysis = analyze_video(task, config)
    published = parse_publication_date(task.get('published_at'))
    description = str(task.get('description') or '').strip()
    tags = {str(tag).lstrip('#').casefold() for tag in (task.get('hashtags') or [])}
    tags.update(extract_hashtags(description))
    content = (description + ' ' + ' '.join(tags)).casefold()
    topic_known = bool(description or tags)
    topic_valid = bool(tags.intersection(config.enabled_hashtags) or
                       any(word.casefold() in content for word in config.keywords))
    date_valid = bool(published and date.fromisoformat(config.min_date) <= published <= date.today())
    missing = []
    if published is None or published > date.today():
        missing.append('date')
    if not topic_known:
        missing.append('topic')
    reason = analysis['reason']
    # A missing date must never hide a known mismatch with task requirements.
    if topic_known and not topic_valid:
        reason = 'В описании нет включённых хештегов или ключевых слов задания'
    preliminary_allowed = bool(missing)
    if published and not date_valid:
        preliminary_allowed = False
    if topic_known and not topic_valid:
        preliminary_allowed = False
    return {
        **analysis, 'eligible': date_valid and topic_valid,
        'needs_review': bool(missing), 'missing': missing, 'reason': reason,
        'date_known': published is not None, 'date_valid': date_valid,
        'topic_known': topic_known, 'topic_valid': topic_valid,
        'preliminary_allowed': preliminary_allowed,
    }
