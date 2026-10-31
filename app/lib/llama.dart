import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api.dart';

/// One Llama build from the server's models/index.json.
class LlamaModel {
  final String id, title, file, sha, note;
  final int bytes, minRam;
  LlamaModel.fromJson(Map<String, dynamic> j)
      : id = j['id'] as String,
        title = j['title'] as String,
        file = j['file'] as String,
        sha = j['sha256'] as String,
        note = j['note'] as String? ?? '',
        bytes = j['bytes'] as int,
        minRam = j['min_ram'] as int? ?? 0;

  String get size => bytes >= 1 << 30
      ? '${(bytes / (1 << 30)).toStringAsFixed(1)} GB'
      : '${(bytes / (1 << 20)).round()} MB';
}

/// Meta's Llama 3.2 on this phone, through the native LiteRT-LM channel.
/// A trimmed copy of the Passaly app's GemmaService.
class Llama {
  Llama._();

  static const _ch = MethodChannel('classes/llama');
  static const _events = EventChannel('classes/llama_events');
  static const _dl = MethodChannel('classes/download');

  /// Partial text while the model writes: a phone CPU can take a while before
  /// the first word, and streaming is how the user tells slow from stuck.
  static Stream<String> get partials => _events.receiveBroadcastStream().map((e) => e as String);

  static Future<int> ramBytes() async {
    try {
      return await _ch.invokeMethod<int>('ramBytes') ?? 0;
    } catch (_) {
      return 0;
    }
  }

  static Future<List<LlamaModel>> catalogue() async =>
      (await Api.models()).map(LlamaModel.fromJson).toList();

  static Future<File> fileFor(LlamaModel m) async =>
      File('${await _dl.invokeMethod<String>('dir')}/${m.file}');

  static Future<List<File>> installed() async {
    final dir = Directory((await _dl.invokeMethod<String>('dir'))!);
    return dir.listSync().whereType<File>().where((f) => f.path.endsWith('.litertlm')).toList();
  }

  static Future<String?> activeName() async =>
      (await SharedPreferences.getInstance()).getString('llama_active');

  static Future<void> setActive(LlamaModel m) async {
    await (await SharedPreferences.getInstance()).setString('llama_active', m.file);
    await unload(); // otherwise the old weights keep answering
  }

  /// The chosen model, or any installed one when the choice is gone.
  static Future<File?> activeFile() async {
    final files = await installed();
    if (files.isEmpty) return null;
    final want = await activeName();
    return files.firstWhere((f) => f.uri.pathSegments.last == want, orElse: () => files.first);
  }

  static Future<bool> hasModel() async => await activeFile() != null;

  /// Handed to Android's DownloadManager: survives the app being closed.
  static Future<int> download(LlamaModel m) async {
    final id = await _dl.invokeMethod<int>('enqueue',
        {'url': '${Api.base}/models/${m.file}', 'file': m.file, 'title': m.title});
    await (await SharedPreferences.getInstance()).setString('llama_pending', '${m.file}|$id');
    return id!;
  }

  static Future<({String file, int id})?> pending() async {
    final raw = (await SharedPreferences.getInstance()).getString('llama_pending');
    if (raw == null) return null;
    final i = raw.lastIndexOf('|');
    return (file: raw.substring(0, i), id: int.parse(raw.substring(i + 1)));
  }

  static Future<void> clearPending() async =>
      (await SharedPreferences.getInstance()).remove('llama_pending');

  /// {state: running|paused|done|failed|gone, downloaded, total}
  static Future<Map<String, dynamic>> status(int id) async =>
      ((await _dl.invokeMethod<Map>('status', {'id': id})) ?? {'state': 'gone'}).cast<String, dynamic>();

  /// A truncated model loads and talks nonsense, so check the published hash.
  static Future<bool> verify(LlamaModel m) async {
    final f = await fileFor(m);
    if (!f.existsSync()) return false;
    final ok = (await sha256.bind(f.openRead()).first).toString() == m.sha;
    if (!ok) f.deleteSync();
    return ok;
  }

  static Future<void> remove(LlamaModel m) async {
    final f = await fileFor(m);
    if (f.existsSync()) f.deleteSync();
    await unload();
  }

  static bool _loaded = false;
  static String? lastError;

  static Future<bool> ensureLoaded() async {
    if (_loaded) return true;
    final f = await activeFile();
    if (f == null) return false;
    try {
      _loaded = await _ch.invokeMethod<bool>('load', {'path': f.path}) ?? false;
    } on PlatformException catch (e) {
      lastError = '${e.code}: ${e.message}';
    }
    return _loaded;
  }

  static Future<void> unload() async {
    _loaded = false;
    try {
      await _ch.invokeMethod('unload');
    } catch (_) {}
  }

  static Future<String> generate(String prompt) async {
    if (!await ensureLoaded()) throw StateError(lastError ?? 'Download a Llama model first');
    return (await _ch.invokeMethod<String>('generate', {'prompt': prompt}) ?? '').trim();
  }

  static Future<void> cancel() async {
    try {
      await _ch.invokeMethod('cancel');
    } catch (_) {}
  }
}

/// This phone's captions, from the native SpeechRecognizer channel.
class Speech {
  Speech._();
  static const _ch = MethodChannel('classes/speech');
  static const _events = EventChannel('classes/speech_events');

  /// {type: listening|partial|final|error, text}
  static Stream<Map> get events => _events.receiveBroadcastStream().map((e) => e as Map);
  static Future<bool> start(String lang) async => await _ch.invokeMethod<bool>('start', {'lang': lang}) ?? false;
  static Future<void> stop() => _ch.invokeMethod('stop');
}
