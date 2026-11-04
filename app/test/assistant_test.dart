import 'package:flutter_test/flutter_test.dart';
import 'package:passaly_classes/assistant.dart';

void main() {
  test('Llama replies: ACTION lines become tasks, junk is dropped', () {
    final r = readAssistant('Ana sends the deck Friday.\n'
        'ACTION {"type":"add_calendar_event","title":"Send deck","start":"2026-11-07T09:00:00-08:00","duration_minutes":9999}\n'
        'ACTION {"type":"add_calendar_event","title":"x","start":"Friday"}\n'
        'ACTION {"type":"delete_everything"}\n'
        'ACTION not json\n'
        'ACTION {"type":"save_note","text":"deck owner: Ana"}');
    expect(r.reply, 'Ana sends the deck Friday.');
    expect(r.actions, [
      {'type': 'add_calendar_event', 'title': 'Send deck', 'start': '2026-11-07T09:00:00-08:00',
       'duration_minutes': 30, 'description': ''},
      {'type': 'save_note', 'text': 'deck owner: Ana'},
    ]);
  });

  test('local time carries its offset', () {
    final s = isoWithOffset(DateTime(2026, 11, 3, 20, 5, 9));
    expect(DateTime.parse(s).isAtSameMomentAs(DateTime(2026, 11, 3, 20, 5, 9)), isTrue);
    expect(RegExp(r'^2026-11-03T20:05:09[+-]\d\d:\d\d$').hasMatch(s), isTrue);
  });

  test('the prompt keeps memory inside the phone budget', () {
    final p = localAssistantPrompt(name: 'Ana', now: 'now', memory: [
      {'title': 'Recap', 'text': 'x' * 5000},
      {'title': 'Other', 'text': 'never reached'},
    ], history: [(role: 'user', text: 'When is the deck due?')]);
    expect(p.contains('never reached'), isFalse);
    expect('x'.allMatches(p).length, lessThan(2600));
    expect(p.trimRight().endsWith('Assistant:'), isTrue);
  });
}
