from datetime import UTC, datetime
from html.parser import HTMLParser

from teleminder.telegram_views import render_reminder, render_tasks


def task(title="test", description="Try the snooze buttons."):
    return {
        "id": 2,
        "title": title,
        "description": description,
        "due_at": "2026-11-18T15:00:00Z",
        "priority": "medium",
        "tags": ["personal"],
        "completed_at": None,
    }


class Balanced(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "code"}
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


def assert_safe(text):
    parser = Balanced()
    parser.feed(text)
    parser.close()
    assert not parser.stack
    assert len(text.encode("utf-16-le")) // 2 <= 4096


def test_reminder_has_clear_title_local_deadline_and_actions_context():
    text = render_reminder(task(), "America/Toronto", now=datetime(2026, 11, 18, 14, 0, tzinfo=UTC))
    assert "<b>test</b>" in text
    assert "10:00 AM" in text and "EST" in text and "Today" in text
    assert "Medium" in text and "#2" in text
    assert_safe(text)


def test_html_content_cannot_inject_tags_or_break_entities():
    text = render_reminder(task("<script>&🧪</script>", "<b>not markup</b>"), "America/Toronto")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&lt;b&gt;not markup&lt;/b&gt;" in text
    assert_safe(text)


def test_extreme_unicode_and_escaped_text_stays_balanced_and_bounded():
    data = task("🧪&<" * 100, "<&🧪" * 1300)
    assert_safe(render_reminder(data, "America/Toronto"))
    rows = [{**data, "id": 2**63 - 1 - i} for i in range(20)]
    text = render_tasks(rows, 20, 1, 1, "America/Toronto")
    assert_safe(text)
    assert "20" in text


def test_empty_list_is_still_a_useful_message():
    text = render_tasks([], 0, 1, 1, "America/Toronto")
    assert "No pending tasks" in text
    assert_safe(text)
