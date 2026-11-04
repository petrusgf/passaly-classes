import 'dart:convert';

import 'tutor.dart';

/// The assistant's tasks, the same for both brains. Muse (cloud) gets them as
/// tools from the server; Llama (this phone) writes them as ACTION lines.
/// Either way they arrive here as maps and run only when the user taps.
const deviceActions = {'add_calendar_event', 'share_text'};

/// The checks the server applies to Muse's calls (`_device_action` in
/// app.py), so a card never shows an event the calendar cannot take.
Map<String, dynamic>? validAction(Map raw) {
  switch (raw['type']) {
    case 'add_calendar_event':
      final title = '${raw['title'] ?? ''}'.trim();
      final start = DateTime.tryParse('${raw['start'] ?? ''}');
      if (title.isEmpty || start == null) return null;
      final m = raw['duration_minutes'];
      return {
        'type': 'add_calendar_event',
        'title': title.length > 200 ? title.substring(0, 200) : title,
        'start': '${raw['start']}',
        'duration_minutes': m is int && m > 0 && m <= 24 * 60 ? m : 30,
        'description': '${raw['description'] ?? ''}',
      };
    case 'add_task':
      final title = '${raw['title'] ?? ''}'.trim();
      if (title.isEmpty) return null;
      final due = '${raw['due'] ?? ''}'.trim();
      final sid = raw['session_id'];
      return {
        'type': 'add_task',
        'title': title.length > 200 ? title.substring(0, 200) : title,
        if (due.isNotEmpty) 'due': due,
        if (sid is int) 'session_id': sid,
      };
    case 'share_text':
    case 'save_note':
      final text = '${raw['text'] ?? ''}'.trim();
      return text.isEmpty ? null : {'type': raw['type'], 'text': text};
  }
  return null;
}

/// Local time as ISO 8601 with its UTC offset ("2026-11-03T20:15:00-08:00"),
/// so both brains read "tomorrow at 9" in the user's own time zone.
String isoWithOffset(DateTime t) {
  final o = t.timeZoneOffset;
  String two(int n) => n.toString().padLeft(2, '0');
  final sign = o.isNegative ? '-' : '+';
  final h = two(o.inHours.abs()), m = two(o.inMinutes.abs() % 60);
  return '${t.year}-${two(t.month)}-${two(t.day)}T${two(t.hour)}:${two(t.minute)}:${two(t.second)}$sign$h:$m';
}

String localAssistantPrompt({
  required String name,
  required String now,
  required List<Map<String, dynamic>> memory,
  required List<({String role, String text})> history,
  List<({int id, String title})> sessions = const [],
}) {
  // ponytail: Llama's context on the phone is 4096 tokens, so memory is cut
  // to about 2500 characters and the chat to the last six turns.
  var budget = 2500;
  final pages = <String>[];
  for (final h in memory) {
    if (budget <= 0) break;
    var t = '${h['text']}';
    if (t.length > budget) t = t.substring(0, budget);
    budget -= t.length;
    pages.add('--- ${h['title']}\n$t');
  }
  final chat = history.length > 6 ? history.sublist(history.length - 6) : history;
  return '''You are $name's assistant for their classes and meetings. Now: $now.
Answer only from MEMORY. If MEMORY does not have it, say you could not find it.
To do something for the user, add one line per task, exactly like these:
ACTION {"type":"add_calendar_event","title":"...","start":"2026-11-07T09:00:00-08:00","duration_minutes":30,"description":"..."}
ACTION {"type":"share_text","text":"..."}
ACTION {"type":"save_note","text":"..."}
ACTION {"type":"add_task","title":"...","due":"...","session_id":12}
A task from a class or meeting takes its session_id from SESSIONS; leave it out for a personal task.
Use the same UTC offset as Now. Reply briefly, in the user's language.

SESSIONS: ${sessions.isEmpty ? 'none' : sessions.take(10).map((s) => '${s.id}: ${s.title}').join('; ')}

MEMORY:
${pages.isEmpty ? '(nothing found)' : pages.join('\n')}

CHAT:
${chat.map((t) => '${t.role == 'user' ? name : 'Assistant'}: ${t.text}').join('\n')}
Assistant:''';
}

/// Llama's reply split into what to say and the tasks it proposed. Lines that
/// are not valid tasks are dropped, never run.
({String reply, List<Map<String, dynamic>> actions}) readAssistant(String raw) {
  final said = <String>[];
  final actions = <Map<String, dynamic>>[];
  for (final line in raw.split('\n')) {
    final t = line.trim();
    if (!t.startsWith('ACTION')) {
      said.add(line);
      continue;
    }
    try {
      final a = validAction(jsonDecode(t.substring(6).trim()) as Map);
      if (a != null) actions.add(a);
    } catch (_) {}
  }
  return (reply: said.join('\n').trim(), actions: actions);
}

String recapPrompt(String kind, String title, List<Line> lines) =>
    '''Write the recap of this $kind, in the language of the transcript, as markdown with exactly these sections:
## Summary
(at most 120 words)
## Decisions
## Action items
(one per line: "- Owner: task (due: date)", the due part only if it was said)
## Open questions
Write "None." under an empty section. Use only the transcript; never invent owners, dates or decisions.

${kind.toUpperCase()}: $title
TRANSCRIPT:
${tail(lines, 6000)}''';
