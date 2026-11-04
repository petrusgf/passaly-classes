import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

/// The class server. It stores classes, lines, questions and profiles. With
/// [engine] 'llama' all AI runs on the phone; with 'cloud' the server asks its
/// own model (Meta's Muse Spark) through /ask.
class Api {
  Api._();

  /// Swapped for a MockClient in tests.
  static http.Client client = http.Client();

  static String base = '';
  static String key = '';
  static String name = '';
  static String engine = 'llama';

  static Future<bool> restore() async {
    final p = await SharedPreferences.getInstance();
    base = p.getString('base') ?? '';
    key = p.getString('key') ?? '';
    name = p.getString('name') ?? '';
    engine = p.getString('engine') ?? 'llama';
    return base.isNotEmpty && key.isNotEmpty;
  }

  /// "192.168.1.20:8010" or a full URL; no trailing slash.
  static String normalize(String raw) {
    var b = raw.trim();
    if (b.isEmpty) return b;
    if (!b.startsWith('http://') && !b.startsWith('https://')) {
      // A laptop on the Wi-Fi (an IP or localhost) is plain HTTP; a domain is
      // HTTPS, whose HTTP side only redirects, which a POST will not follow.
      final local = RegExp(r'^(localhost|[\d.]+)(:\d+)?(/|$)').hasMatch(b);
      b = '${local ? 'http' : 'https'}://$b';
    }
    return b.replaceAll(RegExp(r'/+$'), '');
  }

  static Future<void> signIn(String server, String who) async {
    base = normalize(server);
    final me = await _send('POST', '/api/people', {'name': who}) as Map;
    key = me['key'] as String;
    name = me['name'] as String;
    final p = await SharedPreferences.getInstance();
    await p.setString('base', base);
    await p.setString('key', key);
    await p.setString('name', name);
  }

  static Future<void> signOut() async {
    key = '';
    await (await SharedPreferences.getInstance()).remove('key');
  }

  static Future<dynamic> _send(String method, String path, [Object? body]) async {
    final req = http.Request(method, Uri.parse('$base$path'))
      ..headers['Content-Type'] = 'application/json'
      ..headers['X-Person-Key'] = key;
    if (body != null) req.body = jsonEncode(body);
    final res = await http.Response.fromStream(
        await client.send(req).timeout(const Duration(seconds: 90)));
    final data = res.body.isEmpty ? null : jsonDecode(utf8.decode(res.bodyBytes));
    if (res.statusCode >= 400) {
      throw ApiError(data is Map ? '${data['detail']}' : 'Server error ${res.statusCode}');
    }
    return data;
  }

  /// A GET that keeps its last answer on the phone and falls back to it with
  /// no connection, so classes, their material and the Llama tutor work
  /// offline. A server error (ApiError) is never hidden behind the copy.
  static Future<dynamic> _cachedGet(String path) async {
    final p = await SharedPreferences.getInstance();
    final k = 'cache $base $key $path';
    try {
      final data = await _send('GET', path);
      await p.setString(k, jsonEncode(data));
      flushPending(); // back online: send what waited
      return data;
    } on ApiError {
      rethrow;
    } catch (_) {
      final saved = p.getString(k);
      if (saved == null) rethrow;
      return jsonDecode(saved);
    }
  }

  static Future<List<Map<String, dynamic>>> classes() async =>
      (await _cachedGet('/api/classes') as List).cast<Map<String, dynamic>>();

  /// [kind] is 'class' or 'meeting'.
  static Future<Map<String, dynamic>> create(String title, String subject, String lang,
          [String kind = 'class']) async =>
      await _send('POST', '/api/classes', {'title': title, 'subject': subject, 'lang': lang, 'kind': kind})
          as Map<String, dynamic>;

  /// MCP tokens for AI apps. The token itself comes back only from create.
  static Future<List<Map<String, dynamic>>> mcpTokens() async =>
      (await _send('GET', '/api/mcp/tokens') as List).cast<Map<String, dynamic>>();

  static Future<Map<String, dynamic>> createMcpToken(String name, bool canWrite) async =>
      await _send('POST', '/api/mcp/tokens', {'name': name, 'can_write': canWrite}) as Map<String, dynamic>;

  static Future<void> revokeMcpToken(int id) => _send('DELETE', '/api/mcp/tokens/$id');

  /// Tasks I can see: one class or meeting's, or (no id) all of mine.
  static Future<List<Map<String, dynamic>>> tasks([int? sessionId]) async =>
      (await _cachedGet('/api/tasks${sessionId == null ? '' : '?session_id=$sessionId'}') as List)
          .cast<Map<String, dynamic>>();

  static Future<Map<String, dynamic>> addTask(String title, {String? due, String? assignee, int? sessionId}) async =>
      await _send('POST', '/api/tasks', {
        'title': title,
        if (due != null && due.isNotEmpty) 'due': due,
        if (assignee != null && assignee.isNotEmpty) 'assignee': assignee,
        if (sessionId != null) 'session_id': sessionId,
      }) as Map<String, dynamic>;

  static Future<void> setTaskStatus(int id, String status) =>
      _send('PATCH', '/api/tasks/$id', {'status': status});

  static Future<List<Map<String, dynamic>>> files(int id) async =>
      (await _send('GET', '/api/classes/$id/files') as List).cast<Map<String, dynamic>>();

  /// Raw bytes as the body; the name tells the server the type (.pdf, .txt, .md).
  static Future<void> uploadFile(int id, String name, List<int> bytes) async {
    final res = await http
        .post(Uri.parse('$base/api/classes/$id/files?name=${Uri.encodeQueryComponent(name)}'),
            headers: {'X-Person-Key': key}, body: bytes)
        .timeout(const Duration(seconds: 90));
    if (res.statusCode >= 400) {
      final data = jsonDecode(utf8.decode(res.bodyBytes));
      throw ApiError(data is Map ? '${data['detail']}' : 'Server error ${res.statusCode}');
    }
  }

  /// The host's recap, written on this phone by Llama.
  static Future<void> saveRecap(int id, String text) => _send('PUT', '/api/classes/$id/recap', {'text': text});

  /// The shared memory (ai-memory on the server): the pages that match, in full.
  static Future<List<Map<String, dynamic>>> searchMemory(String q) async =>
      ((await _send('GET', '/api/memory/search?q=${Uri.encodeQueryComponent(q)}') as Map)['hits'] as List)
          .cast<Map<String, dynamic>>();

  static Future<void> saveNote(String text) => _send('POST', '/api/memory/notes', {'text': text});

  /// Cloud mode: Muse answers with tools. {"reply", "actions", "sources"}
  static Future<Map<String, dynamic>> assistant(List<({String role, String text})> history, String now) async =>
      await _send('POST', '/api/assistant', {
        'messages': [for (final t in history) {'role': t.role, 'content': t.text}],
        'now': now,
      }) as Map<String, dynamic>;

  /// Accepts the invite link the web page shows, or just its code.
  static Future<Map<String, dynamic>> join(String linkOrCode) async {
    final code = linkOrCode.trim().split('/').where((s) => s.isNotEmpty).last;
    return await _send('POST', '/api/classes/join/$code') as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> get(int id) async =>
      await _cachedGet('/api/classes/$id') as Map<String, dynamic>;

  static Future<Map<String, dynamic>> lines(int id, int after) async =>
      await _send('GET', '/api/classes/$id/lines?after=$after') as Map<String, dynamic>;

  static Future<void> addLine(int id, String text) => _send('POST', '/api/classes/$id/lines', {'text': text});

  static Future<void> end(int id) => _send('POST', '/api/classes/$id/end');

  static Future<Map<String, dynamic>> pack(int id) async =>
      await _cachedGet('/api/classes/$id/pack') as Map<String, dynamic>;

  static Future<List<Map<String, dynamic>>> questions(int id) async =>
      (await _cachedGet('/api/classes/$id/questions') as List).cast<Map<String, dynamic>>();

  /// Saves a question the phone answered. With no connection it waits on the
  /// phone (returns false) and goes out with the next call that reaches the
  /// server, so the teacher still gets what the class didn't cover.
  static Future<bool> record(int id, String question, String answer, bool fromClass) async {
    final body = {'question': question, 'ai_answer': answer, 'from_class': fromClass};
    try {
      await _send('POST', '/api/classes/$id/questions', body);
      return true;
    } on ApiError {
      rethrow;
    } catch (_) {
      final p = await SharedPreferences.getInstance();
      final q = p.getStringList(_pendingKey) ?? [];
      await p.setStringList(_pendingKey, [...q, jsonEncode({'id': id, ...body})]);
      return false;
    }
  }

  static String get _pendingKey => 'pending $base $key';
  static bool _flushing = false;

  /// Sends queued questions in order; stops at the first one that can't go yet.
  static Future<void> flushPending() async {
    if (_flushing) return;
    _flushing = true;
    try {
      final p = await SharedPreferences.getInstance();
      var q = p.getStringList(_pendingKey) ?? [];
      while (q.isNotEmpty) {
        final r = jsonDecode(q.first) as Map<String, dynamic>;
        try {
          await _send('POST', '/api/classes/${r['id']}/questions',
              {'question': r['question'], 'ai_answer': r['ai_answer'], 'from_class': r['from_class']});
        } on ApiError {
          // The server refused it (e.g. the class is gone): drop it, keep going.
        } catch (_) {
          break; // still offline
        }
        q = q.sublist(1);
        await p.setStringList(_pendingKey, q);
      }
    } finally {
      _flushing = false;
    }
  }

  static Future<void> setEngine(String e) async {
    engine = e;
    await (await SharedPreferences.getInstance()).setString('engine', e);
  }

  /// {"cloud": bool, "model": "muse-spark-1.3" | null}
  static Future<Map<String, dynamic>> tutorInfo() async =>
      await _send('GET', '/api/tutor') as Map<String, dynamic>;

  /// The server's model answers, or sends the question to the teacher.
  static Future<void> ask(int id, String question) =>
      _send('POST', '/api/classes/$id/ask', {'question': question});

  static Future<List<Map<String, dynamic>>> inbox() async =>
      (await _send('GET', '/api/classes/inbox/waiting') as List).cast<Map<String, dynamic>>();

  static Future<void> answer(int questionId, String text) =>
      _send('POST', '/api/classes/questions/$questionId/answer', {'text': text});

  static Future<void> saveProfile(String kind, String text) =>
      _send('PUT', '/api/profiles/$kind', {'text': text});

  static Future<List<Map<String, dynamic>>> models() async {
    final res = await http.get(Uri.parse('$base/models/index.json')).timeout(const Duration(seconds: 15));
    if (res.statusCode != 200) throw ApiError('No models on the server yet');
    return (jsonDecode(res.body)['models'] as List).cast<Map<String, dynamic>>();
  }
}

class ApiError implements Exception {
  final String message;
  ApiError(this.message);
  @override
  String toString() => message;
}
