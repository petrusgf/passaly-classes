import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'assistant.dart';
import 'llama.dart';
import 'main.dart';

const _actions = MethodChannel('classes/actions');

/// Ask about any class or meeting, and let the assistant do things: a
/// calendar event, sharing a recap, a note. The same memory and the same
/// tasks for Muse (cloud) and Llama (this phone); tasks wait for a tap.
class AssistantScreen extends StatefulWidget {
  const AssistantScreen({super.key});
  @override
  State<AssistantScreen> createState() => _AssistantScreenState();
}

class _Turn {
  final String role, text;
  final List<Map<String, dynamic>> actions;
  final Set<int> done = {};
  _Turn(this.role, this.text, [this.actions = const []]);
}

class _AssistantScreenState extends State<AssistantScreen> {
  final _turns = <_Turn>[];
  final _input = TextEditingController();
  final _scroll = ScrollController();
  bool _thinking = false;
  String _streamed = '';

  bool get _cloud => Api.engine == 'cloud';

  Future<void> _send() async {
    final text = _input.text.trim();
    if (text.isEmpty || _thinking) return;
    setState(() {
      _turns.add(_Turn('user', text));
      _input.clear();
      _thinking = true;
      _streamed = '';
    });
    _toEnd();
    final history = [for (final t in _turns) (role: t.role, text: t.text)];
    final now = isoWithOffset(DateTime.now());
    try {
      String reply;
      List<Map<String, dynamic>> actions;
      if (_cloud) {
        final r = await Api.assistant(history, now);
        reply = '${r['reply']}';
        actions = [
          for (final a in (r['actions'] as List).cast<Map>())
            if (a['type'] == 'task_added') a.cast<String, dynamic>() else ?validAction(a),
        ];
      } else {
        final memory = await Api.searchMemory(text).catchError((_) => <Map<String, dynamic>>[]);
        final sessions = await Api.classes()
            .then((cs) => [for (final c in cs) (id: c['id'] as int, title: '${c['title']}')])
            .catchError((_) => <({int id, String title})>[]);
        final sub = Llama.partials.listen((d) => setState(() => _streamed += d));
        final raw = await Llama.generate(
            localAssistantPrompt(name: Api.name, now: now, memory: memory, history: history, sessions: sessions));
        await sub.cancel();
        final r = readAssistant(raw);
        reply = r.reply;
        actions = r.actions;
        // A note is the assistant's own memory, not a change on the phone: saved
        // at once, as the cloud assistant does.
        for (final a in actions.where((a) => a['type'] == 'save_note')) {
          await Api.saveNote('${a['text']}');
        }
        // A task goes straight into the task list too, as with the cloud assistant.
        for (var i = 0; i < actions.length; i++) {
          final a = actions[i];
          if (a['type'] != 'add_task') continue;
          final t = await Api.addTask('${a['title']}', due: a['due'] as String?, sessionId: a['session_id'] as int?);
          actions[i] = {'type': 'task_added', ...t};
        }
      }
      setState(() => _turns.add(_Turn('assistant', reply.isEmpty ? 'Done.' : reply, actions)));
    } catch (e) {
      if (mounted) toast(context, e);
    }
    if (mounted) setState(() => _thinking = false);
    _toEnd();
  }

  void _toEnd() => WidgetsBinding.instance.addPostFrameCallback((_) {
        if (_scroll.hasClients) _scroll.jumpTo(_scroll.position.maxScrollExtent);
      });

  Future<void> _run(_Turn turn, int i) async {
    final a = turn.actions[i];
    var ok = true;
    if (a['type'] == 'add_calendar_event') {
      final start = DateTime.parse('${a['start']}');
      final end = start.add(Duration(minutes: a['duration_minutes'] as int));
      ok = await _actions.invokeMethod<bool>('calendar', {
            'title': a['title'], 'description': a['description'],
            'begin': start.millisecondsSinceEpoch, 'end': end.millisecondsSinceEpoch,
          }) ??
          false;
    } else if (a['type'] == 'share_text') {
      ok = await _actions.invokeMethod<bool>('share', {'text': a['text']}) ?? false;
    }
    if (!ok && mounted) toast(context, 'No app on this phone can do that');
    if (ok) setState(() => turn.done.add(i));
  }

  Widget _actionCard(_Turn turn, int i) {
    final a = turn.actions[i];
    final done = turn.done.contains(i);
    final (IconData icon, String title, String detail, String button) = switch (a['type']) {
      'add_calendar_event' => (
          Icons.event,
          '${a['title']}',
          '${MaterialLocalizations.of(context).formatMediumDate(DateTime.parse('${a['start']}').toLocal())} '
              '${TimeOfDay.fromDateTime(DateTime.parse('${a['start']}').toLocal()).format(context)} · ${a['duration_minutes']} min',
          'Add to calendar'
        ),
      'share_text' => (Icons.share, 'Share', '${a['text']}', 'Share…'),
      'task_added' => (Icons.task_alt, 'Task added', '${a['title']}${a['due'] == null ? '' : ' · due ${a['due']}'}', ''),
      _ => (Icons.bookmark_added, 'Saved to memory', '${a['text']}', ''),
    };
    return Card(
      child: ListTile(
        leading: Icon(icon, color: brand),
        title: Text(title),
        subtitle: Text(detail, maxLines: 3, overflow: TextOverflow.ellipsis),
        trailing: button.isEmpty
            ? null
            : done
                ? const Icon(Icons.check, color: Colors.green)
                : FilledButton(onPressed: () => _run(turn, i), child: Text(button)),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Assistant')),
        body: Column(children: [
          Expanded(
            child: ListView(controller: _scroll, padding: const EdgeInsets.all(16), children: [
              Text(
                  'Ask about any of your classes and meetings, or ask for a reminder, an event or a recap to share. '
                  '${_cloud ? "Thinking in Meta's cloud with Muse Spark." : 'Thinking on this phone with Llama.'} '
                  'Tasks run only when you tap them.',
                  style: Theme.of(context).textTheme.bodySmall),
              const SizedBox(height: 12),
              for (final t in _turns) ...[
                Align(
                  alignment: t.role == 'user' ? Alignment.centerRight : Alignment.centerLeft,
                  child: Container(
                    margin: const EdgeInsets.symmetric(vertical: 4),
                    padding: const EdgeInsets.all(12),
                    constraints: const BoxConstraints(maxWidth: 320),
                    decoration: BoxDecoration(
                      color: t.role == 'user' ? brand.withValues(alpha: 0.12) : Colors.grey.shade100,
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: Text(t.text),
                  ),
                ),
                for (var i = 0; i < t.actions.length; i++) _actionCard(t, i),
              ],
              if (_thinking)
                ListTile(
                  leading: const SizedBox.square(dimension: 20, child: CircularProgressIndicator(strokeWidth: 2)),
                  title: Text(_streamed.isEmpty ? (_cloud ? 'Muse is looking in your memory…' : 'Llama is thinking…') : _streamed),
                ),
            ]),
          ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 4, 12, 12),
              child: Row(children: [
                Expanded(
                  child: TextField(
                    controller: _input,
                    minLines: 1,
                    maxLines: 4,
                    textInputAction: TextInputAction.send,
                    onSubmitted: (_) => _send(),
                    decoration: const InputDecoration(
                        hintText: 'e.g. What did we decide about the budget?', border: OutlineInputBorder()),
                  ),
                ),
                const SizedBox(width: 8),
                IconButton.filled(onPressed: _thinking ? null : _send, icon: const Icon(Icons.send)),
              ]),
            ),
          ),
        ]),
      );
}
