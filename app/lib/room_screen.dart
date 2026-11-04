import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api.dart';
import 'assistant.dart';
import 'llama.dart';
import 'main.dart';
import 'models_screen.dart';
import 'tutor.dart';

/// One class: live transcript with this phone's captions, and the tutor.
/// Every AI step here runs on the phone with Llama; the server only stores.
class RoomScreen extends StatefulWidget {
  final int id;
  const RoomScreen({super.key, required this.id});
  @override
  State<RoomScreen> createState() => _RoomScreenState();
}

class _RoomScreenState extends State<RoomScreen> {
  Map<String, dynamic>? _info;
  final _lines = <Line>[];
  int _last = 0;
  List<Map<String, dynamic>> _questions = [];
  Timer? _poll;

  bool _captions = false;
  String _partial = '';
  StreamSubscription? _speechSub;

  final _ask = TextEditingController();
  bool _thinking = false;
  String _streamed = '';
  String? _working; // what Llama is busy with in the background, shown as a banner

  bool get _teacher => _info?['role'] == 'teacher';
  bool get _meeting => _info?['kind'] == 'meeting';
  String get _what => _meeting ? 'meeting' : 'class';
  bool get _live => _info?['status'] == 'live';
  String get _teacherName => '${_info?['teacher'] ?? 'the teacher'}';

  @override
  void initState() {
    super.initState();
    _start();
  }

  @override
  void dispose() {
    _poll?.cancel();
    _stopCaptions();
    super.dispose();
  }

  Future<void> _start() async {
    try {
      _info = await Api.get(widget.id);
    } catch (e) {
      if (mounted) toast(context, e);
      return;
    }
    await _pull();
    await _loadQuestions();
    _poll = Timer.periodic(const Duration(seconds: 2), (_) => _pull());
    if (!_teacher && !_live) _profileStudentOnce();
  }

  Future<void> _pull() async {
    try {
      final r = await Api.lines(widget.id, _last);
      final fresh = (r['lines'] as List).cast<Map<String, dynamic>>();
      if (!mounted) return;
      setState(() {
        for (final l in fresh) {
          _lines.add(Line.fromJson(l));
          _last = l['id'] as int;
        }
        _info!['status'] = r['status'];
      });
      if (!_live) _poll?.cancel();
    } catch (_) {}
  }

  Future<void> _loadQuestions() async {
    try {
      final q = await Api.questions(widget.id);
      if (mounted) setState(() => _questions = q);
    } catch (_) {}
  }

  // --- captions --------------------------------------------------------------

  Future<void> _toggleCaptions() async {
    if (_captions) return _stopCaptions();
    _speechSub = Speech.events.listen((e) {
      final text = '${e['text']}';
      switch (e['type']) {
        case 'partial':
          setState(() => _partial = text);
        case 'final':
          setState(() => _partial = '');
          Api.addLine(widget.id, text).then((_) => _pull());
        case 'error':
          if (mounted) toast(context, text == 'permission' ? 'Microphone permission is needed' : 'Captions stopped ($text)');
          _stopCaptions();
      }
    });
    if (await Speech.start('${_info!['lang']}')) {
      setState(() => _captions = true);
    } else {
      _stopCaptions();
    }
  }

  void _stopCaptions() {
    _speechSub?.cancel();
    _speechSub = null;
    if (_captions) Speech.stop();
    if (mounted) {
      setState(() {
        _captions = false;
        _partial = '';
      });
    }
  }

  // --- teacher ---------------------------------------------------------------

  Future<void> _end() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('End the $_what?'),
        content: Text('The transcript closes. ${Api.engine == 'cloud' ? "Meta's Muse Spark" : 'Llama on this phone'} '
            'then writes the recap${_meeting ? '' : ' and learns your teaching style'}.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: Text('End $_what')),
        ],
      ),
    );
    if (ok != true) return;
    _stopCaptions();
    await Api.end(widget.id);
    await _pull();
    if (Api.engine == 'cloud') {
      // /end already started the server's recap and profiles with Muse Spark.
      if (mounted) toast(context, 'Muse Spark is writing the recap');
      Future.delayed(const Duration(seconds: 30), _refreshInfo);
      return;
    }
    if (!await Llama.hasModel()) {
      if (mounted) toast(context, 'Download Llama to get a recap');
      return;
    }
    setState(() => _working = 'Llama is writing the recap…');
    try {
      final recap = await Llama.generate(recapPrompt(_what, '${_info!['title']}', _lines));
      if (recap.isNotEmpty) await Api.saveRecap(widget.id, recap);
      await _refreshInfo();
    } catch (e) {
      if (mounted) toast(context, 'Could not write the recap: $e');
    }
    if (_meeting) {
      if (mounted) setState(() => _working = null);
      return;
    }
    if (mounted) setState(() => _working = 'Llama is learning your teaching style…');
    try {
      final pack = await Api.pack(widget.id);
      final style = await Llama.generate(teacherStylePrompt('${pack['my_profile']}', _lines));
      if (style.isNotEmpty) await Api.saveProfile('teacher', style);
      if (mounted) toast(context, 'Your teaching style is saved for the tutor');
    } catch (e) {
      if (mounted) toast(context, 'Could not learn the style: $e');
    }
    if (mounted) setState(() => _working = null);
  }

  // --- student ---------------------------------------------------------------

  /// Once per class, after it ends: what this class shows about how the
  /// student learns, written on the phone and saved for future tutors.
  Future<void> _profileStudentOnce() async {
    final prefs = await SharedPreferences.getInstance();
    final flag = 'profiled_${Api.base}_${widget.id}';
    // In cloud mode the server profiled every student when the class ended.
    if (Api.engine == 'cloud' || prefs.getBool(flag) == true || _lines.isEmpty || !await Llama.hasModel()) return;
    setState(() => _working = 'Llama is getting to know how you learn…');
    try {
      final pack = await Api.pack(widget.id);
      final mine = _questions.map((q) => '${q['question']}').toList();
      final profile = await Llama.generate(studentProfilePrompt('${pack['my_profile']}', Api.name, _lines, mine));
      if (profile.isNotEmpty) await Api.saveProfile('student', profile);
      await prefs.setBool(flag, true);
    } catch (_) {}
    if (mounted) setState(() => _working = null);
  }

  Future<void> _askTutor() async {
    final question = _ask.text.trim();
    if (question.isEmpty || _thinking || _working != null) return;
    setState(() {
      _thinking = true;
      _streamed = '';
    });
    try {
      if (Api.engine == 'cloud') {
        await Api.ask(widget.id, question);
        _ask.clear();
        await _loadQuestions();
        if (mounted) setState(() => _thinking = false);
        return;
      }
      final pack = await Api.pack(widget.id);
      final lines = (pack['lines'] as List).cast<Map<String, dynamic>>().map(Line.fromJson).toList();
      final answers = (pack['teacher_answers'] as List)
          .cast<Map<String, dynamic>>()
          .map((a) => (question: '${a['question']}', answer: '${a['answer']}'))
          .toList();
      final material = retrieve(question, passages(lines, answers, _teacherName));

      var fromClass = false;
      var answer = '';
      if (material.isNotEmpty) {
        if (!await Llama.hasModel()) {
          if (mounted) toast(context, 'Download Llama first: the tutor runs on your phone');
          setState(() => _thinking = false);
          return;
        }
        final sub = Llama.partials.listen((d) => setState(() => _streamed += d));
        final reply = readReply(await Llama.generate(tutorPrompt(
          teacher: _teacherName,
          title: '${_info!['title']}',
          teacherStyle: '${pack['teacher_style']}',
          studentProfile: '${pack['my_profile']}',
          material: material,
          question: question,
        )));
        await sub.cancel();
        (fromClass, answer) = (reply.fromClass, reply.answer);
      }
      if (!fromClass) {
        answer = "That wasn't in this class, so I've sent your question to $_teacherName. "
            "You'll see her answer here.";
      }
      await Api.record(widget.id, question, answer, fromClass);
      _ask.clear();
      await _loadQuestions();
    } catch (e) {
      if (mounted) toast(context, e);
    }
    if (mounted) setState(() => _thinking = false);
  }

  // --- screen ----------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    final info = _info;
    if (info == null) return Scaffold(appBar: AppBar(), body: const Center(child: CircularProgressIndicator()));
    return DefaultTabController(
      length: 2,
      initialIndex: _live || _teacher ? 0 : 1,
      child: Scaffold(
        appBar: AppBar(
          title: Text('${info['title']}', overflow: TextOverflow.ellipsis),
          actions: [
            if (_teacher && _live) TextButton(onPressed: _end, child: Text('End $_what')),
          ],
          bottom: TabBar(tabs: [
            Tab(text: _live ? 'Live' : 'Transcript'),
            Tab(text: _teacher ? 'Questions' : (_meeting ? 'Ask' : 'Tutor')),
          ]),
        ),
        body: Column(children: [
          if (_working != null)
            MaterialBanner(
              content: Text(_working!),
              leading: const SizedBox.square(dimension: 20, child: CircularProgressIndicator(strokeWidth: 2)),
              actions: const [SizedBox.shrink()],
            ),
          Expanded(child: TabBarView(children: [_transcriptTab(), _tutorTab()])),
        ]),
      ),
    );
  }

  Future<void> _refreshInfo() async {
    try {
      final info = await Api.get(widget.id);
      if (mounted) setState(() => _info = info);
    } catch (_) {}
  }

  Widget _transcriptTab() => Column(children: [
        if (_info!['recap'] != null)
          Card(
            margin: const EdgeInsets.all(12),
            child: ExpansionTile(
              initiallyExpanded: true,
              leading: const Icon(Icons.summarize_outlined, color: brand),
              title: const Text('Recap'),
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                  child: SelectableText('${_info!['recap']}'),
                ),
                OverflowBar(children: [
                  TextButton.icon(
                    icon: const Icon(Icons.share),
                    label: const Text('Share'),
                    onPressed: () => const MethodChannel('classes/actions')
                        .invokeMethod('share', {'text': '${_info!['title']}\n\n${_info!['recap']}'}),
                  ),
                ]),
              ],
            ),
          ),
        if (_teacher)
          ListTile(
            dense: true,
            leading: const Icon(Icons.link),
            title: Text('Invite code: ${_info!['invite_token']}'),
            subtitle: Text('${Api.base}/join/${_info!['invite_token']}'),
            trailing: IconButton(
              icon: const Icon(Icons.copy),
              onPressed: () => Clipboard.setData(ClipboardData(text: '${Api.base}/join/${_info!['invite_token']}')),
            ),
          ),
        Expanded(
          child: _lines.isEmpty
              ? const Center(child: Text('No lines yet.'))
              : ListView.builder(
                  reverse: true,
                  padding: const EdgeInsets.all(16),
                  itemCount: _lines.length,
                  itemBuilder: (_, i) {
                    final l = _lines[_lines.length - 1 - i];
                    return Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: Text.rich(TextSpan(children: [
                        TextSpan(
                            text: '${l.name}: ',
                            style: TextStyle(fontWeight: FontWeight.bold, color: l.role == 'teacher' ? brand : null)),
                        TextSpan(text: l.text),
                      ])),
                    );
                  },
                ),
        ),
        if (_partial.isNotEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Text(_partial, style: const TextStyle(fontStyle: FontStyle.italic, color: Colors.grey)),
          ),
        if (_live)
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: FilledButton.icon(
                style: FilledButton.styleFrom(
                    backgroundColor: _captions ? Colors.red : null, minimumSize: const Size.fromHeight(48)),
                onPressed: _toggleCaptions,
                icon: Icon(_captions ? Icons.mic_off : Icons.mic),
                label: Text(_captions ? 'Stop my captions' : 'Start my captions'),
              ),
            ),
          ),
      ]);

  Widget _tutorTab() {
    final label = {
      'answered': ('From the class', Colors.green),
      'waiting_teacher': ('Waiting for $_teacherName', Colors.orange),
      'teacher_answered': ('$_teacherName answered', brand),
    };
    return Column(children: [
      Expanded(
        child: RefreshIndicator(
          onRefresh: _loadQuestions,
          child: ListView(padding: const EdgeInsets.all(16), children: [
            if (!_teacher)
              Text('Your tutor learned from $_teacherName and runs '
                  '${Api.engine == 'cloud' ? "in Meta's cloud with Muse Spark" : 'on this phone with Llama'}. '
                  "If the class didn't cover something, $_teacherName gets your question.",
                  style: Theme.of(context).textTheme.bodySmall),
            if (_thinking)
              Card(
                child: ListTile(
                  leading: const SizedBox.square(dimension: 20, child: CircularProgressIndicator(strokeWidth: 2)),
                  title: Text(_streamed.isEmpty ? (Api.engine == 'cloud' ? 'Muse Spark is reading the class…' : 'Llama is reading the class…') : _streamed),
                ),
              ),
            if (_questions.isEmpty && !_thinking)
              const Padding(padding: EdgeInsets.all(16), child: Text('No questions yet.')),
            for (final q in _questions)
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Row(children: [
                      Expanded(
                          child: Text('${_teacher ? '${q['student']}: ' : ''}${q['question']}',
                              style: const TextStyle(fontWeight: FontWeight.w600))),
                      Chip(
                        visualDensity: VisualDensity.compact,
                        label: Text(label[q['status']]!.$1, style: const TextStyle(fontSize: 11)),
                        side: BorderSide(color: label[q['status']]!.$2),
                      ),
                    ]),
                    Text('${q['ai_answer']}'),
                    if (q['teacher_answer'] != null) ...[
                      const SizedBox(height: 8),
                      Container(
                        padding: const EdgeInsets.only(left: 8),
                        decoration: const BoxDecoration(border: Border(left: BorderSide(color: brand, width: 3))),
                        child: Text('$_teacherName: ${q['teacher_answer']}'),
                      ),
                    ],
                  ]),
                ),
              ),
          ]),
        ),
      ),
      if (!_teacher)
        SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Row(children: [
              Expanded(
                child: TextField(
                  controller: _ask,
                  minLines: 1,
                  maxLines: 3,
                  decoration: const InputDecoration(hintText: 'Ask your tutor', border: OutlineInputBorder()),
                ),
              ),
              const SizedBox(width: 8),
              IconButton.filled(
                onPressed: _thinking || _working != null ? null : _askTutor,
                icon: const Icon(Icons.send),
              ),
            ]),
          ),
        ),
      if (!_teacher)
        TextButton(
          onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ModelsScreen())),
          child: const Text('Llama on this phone'),
        ),
    ]);
  }
}
